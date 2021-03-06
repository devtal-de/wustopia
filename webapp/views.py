#!/usr/bin/python
# -*- coding: utf-8 -*-
from flask import render_template, request, Response, redirect, abort
from flask_babel import gettext
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.orm import joinedload
from sqlalchemy import func
from datetime import datetime, timedelta
from rq import Queue
from rq.job import Job
from worker import conn

import json
import os

from webapp import app
from webapp.models import *
from webapp.places import *
from webapp.forms import *
from webapp.finance import *
from webapp.achievements import *

def wustopia_render_template(template, **kwargs):
    if not 'UserLoginForm' in kwargs:
        kwargs['UserLoginForm']=UserLoginForm()
    if not 'UserCreateForm' in kwargs:
        kwargs['UserCreateForm']=UserCreateForm()
    return render_template(template, **kwargs)

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect('/map')
    return wustopia_render_template('index.html')


@app.route("/map")
@login_required
def map():
    return wustopia_render_template('map.html')

@app.route("/imprint")
def imprint():
    return wustopia_render_template('imprint.html')

@app.route("/ranking")
def ranking():
    return wustopia_render_template('ranking.html', PlaceCategory = db.session.query(PlaceCategory).all())

@app.route("/achievements")
@login_required
def achievements():
    achievements = db.session.query(AchievementsCollected).filter_by(user_id=current_user.id).order_by(AchievementsCollected.reached)
    return wustopia_render_template('achievements.html', achievements=achievements )

@app.route("/api/resources")
@login_required
def api_resources():
    output = []
    resources = getBalance(current_user.id)
    for resource in resources:
        item = {}
        item['id'] = resource.resource.id
        item['name'] = gettext("#%s" % resource.resource.name)
        item['image'] = resource.resource.image
        item['major'] = resource.resource.major
        item['amount'] = resource.amount
        output.append(item)

    response = Response( json.dumps(output) )
    response.headers.add('Content-Type', "application/json")
    return response

@login_required
@app.route("/build")
def build():
    #TODO:Do some checks!
    place = db.session.query(Place).options(joinedload(Place.placecategory)).filter_by(id = request.args.get('place')).first()
    building = db.session.query(Built).filter_by(place_id = request.args.get('place'), user_id = current_user.id).first()
    buildinglevel = building.level+1 if building else 1
    buildcost = db.session.query(BuildCostResource).options(joinedload(BuildCostResource.buildcost)).filter(BuildCost.placecategory_id == place.placecategory.id, BuildCost.level==buildinglevel, BuildCostResource.buildcost_id==BuildCost.id).all()

    #if there aren't buildcost defined, return
    if not buildcost:
        abort(404)

    for costs in buildcost:
        current_balance = getBalanceofResource(current_user.id, costs.resource.id)
        if not current_balance:
            return gettext("#You don't have ") + gettext("#%s" % costs.resource.name), 500
        if current_balance.amount >= costs.amount:
            current_balance.amount -= costs.amount
            db.session.add(current_balance)
        else:
            #return str(getBalanceofResource(current_user.id, costs.resource.id).amount) + " >= " + str(costs.amount)
            return gettext("#You don't have enough %(a)s. (%(b)s/%(c)s)", a=gettext("#%s" % costs.resource.name), b=getBalanceofResource(current_user.id, costs.resource.id).amount, c=costs.amount), 500

    buildtime = db.session.query(BuildCost).filter(BuildCost.placecategory_id == place.placecategory.id, BuildCost.level==buildinglevel).first().time
    ready = datetime.now() + timedelta(seconds = buildtime)
    if building:
        if building.ready >= datetime.now():
            return gettext("#You are already building"), 500
        building.level = building.level+1
        building.ready = ready
        building.lastcollect = datetime.now()
    else:
        db.session.add(Built(place_id = request.args.get('place'), user_id = current_user.id, lastcollect = datetime.now(), ready=ready))
    try:
        db.session.commit()
        #check for new achievements
        api_check_achievements()
        return gettext('#success')
    except Exception as e:
        db.session.rollback()
        if app.debug:
            print(e)
        return gettext('#unkown Error: %(e)s', e=e), 500

@login_required
@app.route("/collect_all")
def collect_all():
    nodes = getUserPlaces(current_user.id)
    for node in nodes:
        if timedelta(minutes=node.PlaceCategoryBenefit.interval) <= datetime.now() - node.Built.lastcollect:
            earn(node.Place.id)
            #TODO reduce Klabimbis
    return gettext('#success')

@login_required
@app.route("/earn/<int:place_id>")
def earn(place_id):
    something_changed=False
    building = db.session.query(Built).options(joinedload(Built.place)).filter_by(place_id = place_id, user_id = current_user.id).first()
    buildingbenefit = db.session.query(PlaceCategoryBenefit).filter_by(placecategory_id = building.place.placecategory.id, level=building.level).all()
    for benefit in buildingbenefit:
        if timedelta(minutes=benefit.interval) <= datetime.now() - building.lastcollect:
            current_balance = db.session.query(Balance).options(joinedload(Balance.resource)).filter_by(user_id=current_user.id, resource_id=benefit.resource.id).first()
            if current_balance:
                #update
                current_balance.amount = Balance.amount + benefit.amount
            else:
                #new entry
                current_balance = Balance(user_id=current_user.id, resource_id=benefit.resource.id, amount=benefit.amount)
            building.lastcollect = datetime.now()
            db.session.add(current_balance)
            db.session.add(building)
            something_changed=True
    if something_changed:
        try:
            db.session.commit()
            return gettext('#success')
        except Exception as e:
            db.session.rollback()
            if app.debug:
                print(e)
            return gettext('#unkown Error: %(e)s', e=e), 500
    return ""

@app.route("/api/markerIcon")
def markerIcon():
    output = []
    categories = db.session.query(PlaceCategory)
    for marker in categories:
        item = {}
        item['id'] = marker.id
        item['name'] = gettext("#%s" % marker.name)
        if marker.icon:
            item['icon'] = marker.icon
        else:
            item['icon'] = "home"
        item['markerColor'] = marker.markerColor
        item['prefix'] = 'fa'
        output.append(item)

    response = Response( json.dumps( output ) )
    response.headers.add('Content-Type', "application/json")
    return response

@app.route("/api/places")
@login_required
def api_places():
    lat = float(request.args.get('lat') or 0)
    lon = float(request.args.get('lon') or 0)
    response = Response( json.dumps(getPlaces(lat, lon ) ) )
    response.headers.add('Content-Type', "application/json")
    return response

@app.route("/api/version")
def api_version():
    if os.path.isfile("version.txt"):
        file = "version.txt"
    else:
        file =".git/refs/heads/master"
    with open(file) as f:
        content = f.read()
    response = Response( content )
    response.headers.add('Content-Type', "application/json")
    return response

@app.route("/api/check_achievements")
def api_check_achievements():
    if app.config['TESTING']:
        return check_achievements(current_user.id)
    else:
        q = Queue("achievements",connection=conn)
        job = q.enqueue_call(
            func=check_achievements, args=(current_user.id,), result_ttl=60 # 10 minute
        )
        return job.get_id()


@app.route("/update_places/<float:lat1>,<float:lon1>,<float:lat2>,<float:lon2>")
def update_places(lat1,lon1,lat2,lon2):
    if app.config['TESTING']:
        return importPlaces(lat1,lon1,lat2,lon2)
    else:
        q = Queue("update_places",connection=conn)
        job = q.enqueue_call(
            func=importPlaces, args=(lat1,lon1,lat2,lon2), result_ttl=600 # 10 minute
        )
        return job.get_id()
        #return "<a href=\"/update_places/"+job.get_id()+"\">result</a>"

@app.route("/update_places/<job_key>")
def get_results(job_key):
    job = Job.fetch(job_key, connection=conn)
    if job.is_finished:
        return gettext(job.result), 200
    else:
        return gettext("#not yet"), 202


@app.route("/help")
def help():
    return wustopia_render_template('help.html', PlaceCategory = db.session.query(PlaceCategory).all())


@app.route("/help/building/<id>-<slug>")
def help_building(id,slug):
    costs = db.session.query(BuildCost, BuildCost, Resource, BuildCostResource) \
        .join(BuildCostResource.buildcost) \
        .join(Resource, BuildCostResource.resource) \
        .filter(BuildCost.placecategory_id == id) \
        .order_by(db.asc('level')) \
        .all()

    benefit = db.session.query(PlaceCategoryBenefit, Resource) \
        .join(Resource) \
        .filter(PlaceCategoryBenefit.placecategory_id == id) \
        .order_by(db.asc('level')) \
        .all()

    placecategory = db.session.query(PlaceCategory) \
        .filter(PlaceCategory.id == id) \
        .first()

    if not placecategory:
        abort(404)
    return wustopia_render_template('help_building.html', costs=costs, benefit=benefit, placecategory=placecategory)

@app.route("/ranking/building/<id>")
@app.route("/ranking/building/<id>-")
@app.route("/ranking/building/<id>-<slug>")
def ranking_building(id,slug=None):
    ranking = db.session.query(func.count(Built.id).label('count'), User) \
        .join(User) \
        .join(Place) \
        .filter(Place.placecategory_id == id) \
        .group_by(User.id) \
        .order_by(db.desc('count')) \
        .limit(50) \
        .all()

    placecategory = db.session.query(PlaceCategory) \
        .filter(PlaceCategory.id == id) \
        .first()

    #if there aren't any rankings, return
    #if not ranking:
    #    abort(404)
    return wustopia_render_template('ranking_building.html', ranking=ranking, placecategory=placecategory)


@app.route('/demo', methods=['POST'])
def demo():
    user = User.new_user()
    login_user(user)
    return redirect('/map')

@app.route('/user/create', methods=['POST'])
def user_create():
    try:
        form = UserCreateForm()
        if form.validate_on_submit():
            user = User.new_user(
                username = form.username.data,
                email = form.email.data,
                password = form.password.data
            )
            login_user(user, remember=True)
            return redirect('/map')
        return wustopia_render_template("error.html", error = form.errors)
    except Exception as e:
        return wustopia_render_template("error.html", error=e)

@app.route('/user/login', methods=['POST','GET'])
def user_login():
    form = UserLoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if not user or not user.is_correct_password(form.password.data):
            return wustopia_render_template("error.html", error=gettext("#wrong User / Password"))
        login_user(user, remember=True)
        return redirect('/map')
    return wustopia_render_template("error.html",error=gettext("#Did you fill all fields?"))

@app.route('/user/logout')
def user_logout():
    logout_user()
    return redirect('/')
