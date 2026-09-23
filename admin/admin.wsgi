#! /usr/bin/env python3
import os, sys
import json
import pyinotify
import redis

from functools import reduce
from urllib.parse import parse_qs

# for local testing
private = '/web/groups/' + os.environ['USER'] + '/private/queup/'

sys.path.append(os.environ['PWD'] + '/queup')
from lib.lock import *
from lib.methods import getowners, getsections, getrooms
from lib.ratelimiter import *
from lib.wsgidefs import *

sys.path.append(os.environ['PWD'] + '/queup/admin')
from alib.methods import *

def application(environ, strp):
    # initialize some variables
    user = environ.get('REMOTE_USER', "")
    query = parse_qs(environ['QUERY_STRING'])
    query = {k: v[0] for k, v in query.items()}
    
    # now check our variables
    accepted_keys = ['sseupdate', 'log', 'fulllog', 'section']
    querychecked = any([x in query for x in accepted_keys]) and 'room' in query
    room = query.get("room", "")
    
    # don't run unless redis is up
    try:
        rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())
    except:
        return ret_500(strp, "Database connection failed.  Contact course staff **immediately**.")
    
    # check if room exists
    if not rds.get("room" + room):
        return ret_404(strp, "Room does not exist")
    
    # if user is not owner of room (or has section 0), return 403 
    # (401 triggers auth popup in browser)
    owners = getowners(room) + [x for x in getsections(room) if getsections(room)[x] == "0"]
    if user not in owners:
        return ret_403(strp, "You are not authorized to view this room")
    
    # this section handles enabling any student to join a room
    if querychecked and 'log' in query:
        room = query.get("room", "")
        return ret_json(strp, json.dumps(getdblog(room)[-50:]))
    elif querychecked and 'fulllog' in query:
        room = query.get("room", "")
        return ret_json(strp, json.dumps(getdblog(room)))
    elif querychecked and 'section' in query:
        room = query.get("room", "")
        if environ.get('REQUEST_METHOD', 'GET') == 'POST':
            try:
                length = int(environ.get('CONTENT_LENGTH', 0) or 0)
            except ValueError:
                length = 0
            data = environ['wsgi.input'].read(length).decode('utf-8', errors='replace') if length else ""
        else:
            data = ""
        if data == "":
            # this is a request to get the section data
            return ret_json(strp, json.dumps({"status": "success", "data": sectiondata(room, "", "get")}))
        else:
            # this is a request to set the section data
            try:
                ret = sectiondata(room, data, "set")
                if ret:
                    return ret_json(strp, json.dumps({"status": "success"}))
                else:
                    return ret_json(strp, json.dumps({"status": "error"}))
            except:
                return ret_json(strp, json.dumps({"status": "fatalerror"}))
    elif querychecked and 'sseupdate' in query:
        return sseupdate(environ, strp)
    # invalid request
    else:
        return ret_400(strp, "Invalid request")


def sseupdate(environ, start_response):
    global private
    query = parse_qs(environ['QUERY_STRING'])
    query = {k: v[0] for k, v in query.items()}
    rooms = getrooms()

    room = query.get('room', '')
    if room == '' or "room"+room not in rooms:
        sys.stderr.write("Room %s not found in database\n" % room)
        return ret_400(start_response, "Room not found in database.")

    start_response('200 OK', [
        ('Content-Type', 'text/event-stream;charset=utf-8'),
        ('Cache-Control', 'no-cache;public'),
        ('Connection', 'keep-alive'),
    ])

    yield ("data: %s\n\n" % json.dumps(getdblog(room)[-50:])).encode()
    # this time, add a timeout so it doesn't hang forever
    rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip(), socket_timeout=30)
    try:
        with rds.monitor() as m:
            for command in m.listen():
                cmd = command['command'].lower()
                if "del room"+room.lower() in cmd or "set room"+room.lower() in cmd:
                    try:
                        yield ("data: %s\n\n" % json.dumps(getdblog(room)[-50:])).encode()
                    except:
                        try:
                            sys.exit(0)
                        except:
                            os._exit(0)
    except: # includes redis.TimeoutError
        try:
            return ("data: %s\n\n" % json.dumps(getdblog(room)[-50:])).encode()
        except:
            try:
                sys.exit(0)
            except:
                os._exit(0)

MIDDLEWARES = [ ]
app = reduce(lambda h, m: m(h), MIDDLEWARES, application)