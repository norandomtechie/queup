#! /usr/bin/env python3

import os
import json
import re
import sys

from time import time
from functools import reduce
from urllib.parse import parse_qs

import redis # type: ignore

# grab config based on IP and init all variables
private = '/web/groups/' + os.environ['USER'] + '/private/queup/'

sys.path.append(os.environ['PWD'] + '/queup')
from lib.lock import *
from lib.methods import *
from lib.ratelimiter import *
from lib.wsgidefs import *

def application(environ, start_response):
    global ip, private, rds

    # grab config based on IP and init all variables
    private = environ['DOCUMENT_ROOT'] + environ['CONTEXT_PREFIX'].replace('~', '') + '/private/queup/'

    # don't run unless redis is up
    try:
        rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())
    except:
        return ret_500(start_response, "Database connection failed.  Contact course staff **immediately**.")
    
    # initialize some variables from wsgi environment
    user = environ.get('REMOTE_USER', "").lower()
    if user == "":
        return ret_401(start_response, "No user provided.")
    
    # check rate limit for this user
    with RateLimiter(private + "ratelimit.db", environ) as rl:
        if rl.should_limit(user):
            return ret_429(start_response, "Rate limit exceeded.")

    # add a ?test=1 route
    if 'QUERY_STRING' in environ and environ['QUERY_STRING'] == 'test=1':
        return ret_ok(start_response, "OK")

    query = parse_qs(environ['QUERY_STRING'])
    # change values to strings
    query = {k: v[0] for k, v in query.items()}
    ip = environ['REMOTE_ADDR']

    room = query.get('room', '')
    if not re.search(ROOM_RGX, room):
        return ret_400(start_response, "Invalid room name " + room)
    
    action = query.get('action', '')
    actions = ['add', 'del', 'chk', 'ren', 'own', 'delown', 'setcool', 'setsub', 'lock', 'unlock', 'clear', 'mark', 'setperm', 'tgl1q', 'timer', 'broadcast']
    if 'sseupdate' not in query and not (action in actions):
        return ret_400(start_response, "Invalid action " + action)
    setup = query.get('setup', '')
    roomsetup = (setup != '') and (action != '') and (room != '') and 'queue' not in query
    queuesetup = (setup != '') and (action != '') and (room != '') and 'queue' in query
    querychecked = setup == '' and (action != '') and (room != '') and 'queue' in query

    # is the room valid?
    if rds.exists("room"+room) == 1:
        owners = getowners(room)
        is_section0 = getsectionforuser(user, room)
        is_owner = (user in owners) or (is_section0 == "0") or (user == 'menon18')
        valid_actions = [
            # only check if room exists, allowed for non-owner
            action in ['chk'],   
            # watch room, allowed for non-owner
            'sseupdate' in query,  
            # add/del to a queue, allowed for non-owner
            (action in ['add', 'del'] and querychecked)  
        ]
        # if db exists, but we're not add/del to a queue, and we're not owner, then bad request.
        if not any(valid_actions) and not is_owner:
            return ret_400(start_response, "Illegal action for non-owner.")
    # or, are we creating a room?
    elif roomsetup and action == 'add': 
        is_owner = True
    # so no db exists, and we're not creating one.
    # what on earth are you doing?
    else:
        return ret_400(start_response, "Malformed request, no room " + room)
    
    newusers = query.get('newusers', '')
    subtitle = query.get('subtitle', '')

    # now check our variables
    rooms = getrooms()

    # set section
    section = getsectionforuser(user, room)
    
    ### ADDED: Logic variables for new features
    will_timer = action == 'timer' and "room"+room in rooms
    will_broadcast = action == 'broadcast' and "room"+room in rooms
    ### END ADDED

    # this section handles adding and removing in a room
    if roomsetup:
        # check actions based on whether adding/deleting
        will_add     = action == 'add'  # room should not be in the database already
        will_del     = action == 'del' and "room"+room in rooms # room is in the database
        will_chk     = action == 'chk' and "room"+room in rooms # room is in the database
        will_own     = action == 'own' and "room"+room in rooms # room is in the database
        will_own     = will_own and (newusers == "" or all([re.match(USER_RGX, x) for x in newusers.split(",")]))
        will_delown  = action == 'delown' and "room"+room in rooms # room is in the database
        will_delown  = will_delown and (newusers == "" or all([re.match(USER_RGX, x) for x in newusers.split(",")]))
        will_setsub  = action == 'setsub' and "room"+room in rooms # room is in the database
        will_setsub  = will_setsub and (subtitle == "" or re.match(SUBTITLE_RGX, subtitle))
        will_tgllock = action in ['lock', 'unlock'] and "room"+room in rooms # room is in the database
        will_setcool = action == 'setcool' and "room"+room in rooms # room is in the database
        will_setperm = action == 'setperm' and "room"+room in rooms # room is in the database
        will_tgl1q   = action == 'tgl1q' and "room"+room in rooms # room is in the database
        # perform the action
        if will_add:
            try:
                createroom(room, user)
            except Exception as e:
                return ret_400(start_response, str(e))
            rooms = getrooms()
            userdata = getusers("", room)
            userdata["is-owner"] = is_owner
            userdata["section"] = section
            userdata["cooldown"] = getcooldown(room)
            if is_owner:
                userdata["owners"] = getowners(room)
            userdata["subtitle"] = ""
            userdata["is-locked"] = False
            # it is possible to define a room first before creating it, so check permanency anyway
            userdata["is-permanent"] = getroompermanency(room)
            userdata["is-singlequeue"] = get1q(room)
            lockAndWriteLog(room, ",".join([str(time()), user, "rcreate", room]))
            return ret_json(start_response, json.dumps(userdata))
        elif will_chk:
            if "room"+room not in rooms:
                return ret_400(start_response, "Room not found in database. May be misconfigured." % room)
            userdata = getusers("", room)
            userdata["is-owner"] = is_owner
            userdata["section"] = section
            userdata["cooldown"] = getcooldown(room)
            if is_owner:
                userdata["owners"] = getowners(room)
            userdata["subtitle"] = getroomsubtitle(room)
            userdata["is-locked"] = isroomlocked(room)
            userdata["is-permanent"] = getroompermanency(room)
            userdata["is-singlequeue"] = get1q(room)
            if "admin" not in query or not is_owner:
                lockAndWriteLog(room, ",".join([str(time()), user, "rchk", room]))
            return ret_json(start_response, json.dumps(userdata))
        ### ADDED: New handlers for Timer and Broadcast
        elif will_timer:
            if not is_owner: return ret_401(start_response, "Unauthorized")
            op = query.get('op', '')
            duration = query.get('duration', None)
            try:
                updatetimer(room, op, duration)
                lockAndWriteLog(room, ",".join([str(time()), user, "rtimer", room, op]))
                return ret_ok(start_response, "success")
            except Exception as e:
                return ret_400(start_response, str(e))
                
        elif will_broadcast:
            if not is_owner: return ret_401(start_response, "Unauthorized")
            msg = query.get('message', '')
            try:
                sendbroadcast(room, user, msg)
                lockAndWriteLog(room, ",".join([str(time()), user, "rbcast", room]))
                return ret_ok(start_response, "success")
            except Exception as e:
                return ret_400(start_response, str(e))
        ### END ADDED
        else:
            if not is_owner:
                return ret_401(start_response, "User is not an owner of room.")
            try:
                if will_del:
                    if getroompermanency(room):
                        return ret_400(start_response, "Room is permanent.")
                    deleteroom(room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "rdel", room]))
                    return ret_json(start_response, json.dumps({"status": "success"}))
                elif will_own:
                    ownroom(room, newusers)
                    lockAndWriteLog(room, ",".join([str(time()), user, "rown", room, newusers]))
                    return ret_json(start_response, json.dumps(getowners(room)))
                elif will_delown:
                    delownroom(room, newusers)
                    lockAndWriteLog(room, ",".join([str(time()), user, "rdelown", room, newusers]))
                    return ret_json(start_response, json.dumps(getowners(room)))
                elif will_setsub:
                    setroomsubtitle(room, subtitle)
                    lockAndWriteLog(room, ",".join([str(time()), user, "rsub", room, subtitle]))
                    return ret_json(start_response, json.dumps({"status": "success"}))
                elif will_tgllock:
                    if action == 'unlock':
                        unlockroom(room)
                        lockAndWriteLog(room, ",".join([str(time()), user, "runlock", room]))
                    else:
                        lockroom(room)
                        lockAndWriteLog(room, ",".join([str(time()), user, "rlock", room]))
                    return ret_json(start_response, json.dumps({"status": "success"}))
                elif will_setcool:
                    cooldown = int(query.get('cooldown', ''))
                    setcooldown(cooldown, room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "rcool", room, str(cooldown)]))
                    return ret_json(start_response, json.dumps({"status": "success"}))
                elif will_setperm:
                    if user == 'menon18':
                        setroompermanency(not getroompermanency(room), room)
                        lockAndWriteLog(room, ",".join([str(time()), user, "rperm", room, str("true")]))
                        return ret_json(start_response, json.dumps({"status": "success", "perm": str(getroompermanency(room))}))
                    else:
                        return ret_401(start_response, "This ability is restricted to developers.")
                elif will_tgl1q:
                    set1q(not get1q(room), room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "r1q", room, str("true")]))
                    return ret_json(start_response, json.dumps({"status": "success", "1q": str(get1q(room))}))
                else:
                    return ret_400(start_response, "No valid query.")
            except redis.RedisError as e:  # our fault
                return ret_500(start_response, "RedisError running action: " + str(e))
            except Exception as e: # their fault
                return ret_400(start_response, "Unrecognized error running action: " + str(e))
    elif queuesetup:
        if not is_owner:
            return ret_401(start_response, "User is not an owner of room.")
        room = query.get('room', '')
        queue = query.get('queue', '')
        if len(room) != 5 or not re.search(ROOM_RGX, room):
            return ret_400(start_response, "Invalid room name.")
        if not re.search(QUEUE_RGX, queue):
            return ret_400(start_response, "Invalid room name.")
        queue = query.get('queue', '')
        newqueue = query.get('newqueue', '')
        username = query.get('username', '')
        # check actions based on whether adding/deleting/checking/renaming
        will_add = query.get('action', '') == 'add'
        will_del = query.get('action', '') == 'del' and "room"+room in rooms # room is in the database
        will_del = will_del and queue in getqueues(room) # queue was in the database
        will_chk = query.get('action', '') == 'chk' and "room"+room in rooms # room is in the database
        will_chk = will_chk and queue in getqueues(room) # queue was in the database
        will_ren = query.get('action', '') == 'ren' and "room"+room in rooms # room is in the database
        will_ren = will_ren and queue in getqueues(room) # queue was in the database
        will_ren = will_ren and re.match(QUEUE_RGX, newqueue) and newqueue not in getqueues(room) # new queue must not already exist and newqueue != ''
        will_clear = query.get('action', '') == 'clear' and "room"+room in rooms # room is in the database
        will_clear = will_clear and queue in getqueues(room) # queue was in the database
        will_mark = query.get('action', '') == 'mark' and "room"+room in rooms # room is in the database
        will_mark = will_mark and queue in getqueues(room) and any([username == x[0] for x in getusers(queue, room)])   # queue was in the database and user is in the queue
        # perform the action
        if will_add or will_del or will_ren or will_clear or will_mark:
            try:
                if will_add:
                    try:
                        createqueue(queue, room)
                    except redis.RedisError as e:
                        if "already exists" in str(e):
                            pass
                    lockAndWriteLog(room, ",".join([str(time()), user, "qadd", room, queue]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                elif will_del:
                    # queue cannot be the only queue in the room!
                    if len(getqueues(room)) == 1:
                        return ret_400(start_response, "Cannot delete the only queue in a room.")
                    deletequeue(queue, room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "qdel", room, queue]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                elif will_ren:
                    renamequeue(queue, newqueue, room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "qren", room, queue, newqueue]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                elif will_clear:
                    # get all users and remove them from the queue
                    for u in getusers(queue, room):
                        delquser(u[0], queue, room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "qclr", room, queue]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                elif will_mark:
                    # toggle mark on user
                    togglemark(username, queue, room)
                    lockAndWriteLog(room, ",".join([str(time()), user, "qmrk", room, queue, username]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                else:
                    return ret_400(start_response, "No valid query.")
            except redis.RedisError as e: 
                return ret_500(start_response, "RedisError: " + str(e))
            except Exception as e:
                import traceback
                sys.stderr.write(traceback.format_exc())
                return ret_ok(start_response, str(e))
        elif will_chk:
            return ret_ok(start_response, json.dumps(getusers("", room)))
        else:
            return ret_400(start_response, "Invalid action.")
    elif querychecked:
        room = query.get('room', '')
        queue = query.get('queue', '')
        username = query.get('username', '')
        if room == '' or "room"+room not in rooms or queue == '' or queue not in getqueues(room):
            return ret_400(start_response, "Invalid room/queue name.")
        # room name should already be in database
        db_queue = getusers(queue, room)
        # check if room is locked before adding anyone unless we are owner
        # perform actions based on whether adding/deleting (will_del does not include owner deleting users)
        will_add = action == 'add' and not any([user == x[0] for x in db_queue]) # username should not be in the room already
        will_del = action == 'del' and any([user == x[0] for x in db_queue]) and (username == '') # username was in the room and is not someone else or empty
        staff_del = is_owner and action == 'del' and username is not None and any([username == x[0] for x in db_queue]) # username was in the room
        if isroomlocked(room) and not is_owner and will_add:
            return ret_423(start_response, "Room is locked.")
        # only make changes to database if changes are to be made
        try:
            if will_add:
                # check if there is a cooldown period, and that user is not adding themselves more than
                # COOLDOWN minutes since their last add
                cooldown = getcooldown(room)
                if cooldown > 0:
                    lastadd = getlastadd(room, user)
                    if (time() - lastadd) < (cooldown * 60):
                        rem_min = int(cooldown - (time() - lastadd) / 60)
                        rem_sec = int((cooldown - (time() - lastadd) / 60) % 1 * 60)
                        return ret_ok(start_response, "[cooldown] This room only permits you to add yourself to any queue every %d minutes. Please wait %d minutes and %s seconds before adding yourself again." % (cooldown, rem_min, rem_sec))
                if get1q(room):
                    # ensure that user is not in any other queues
                    for q in getqueues(room):
                        if any([user == x[0] for x in getusers(q, room)]):
                            return ret_ok(start_response, "[singlequeue] You are already in a queue.")
                waitdata = query.get('waitdata', '')
                if waitdata != '' and not re.match(WAITDATA_RGX, waitdata):
                    return ret_400(start_response, "Invalid waitdata.")
                addquser(user, waitdata, queue, room)
                lockAndWriteLog(room, ",".join([str(time()), user, "uadd", room, queue, waitdata]))
                ret_ok(start_response, "success")
            elif will_del:
                delquser(user, queue, room)
                lockAndWriteLog(room, ",".join([str(time()), user, "udel", room, queue]))
                ret_ok(start_response, "success")
            elif staff_del:
                # owner is removing someone from the queue
                delquser(username, queue, room)
                lockAndWriteLog(room, ",".join([str(time()), user, "usdel", room, queue, username]))
                ret_ok(start_response, "success")
            else:
                return ret_400(start_response, "Invalid action.")
        except redis.RedisError as e: 
            return ret_500(start_response, "RedisError: " + str(e))
        except Exception as e:
            return ret_400(start_response, str(e))
        return ret_ok(start_response, "success")
    #
    # Otherwise it is waiting for updates
    #
    elif 'sseupdate' in query:
        return sseupdate(environ, start_response)
    else:
        return ret_400(start_response, "Invalid request.")

# made sseupdate a separate function because you cannot mix "yield" and "return" 
# in the same function if you want a working WSGI app
# "yield" is necessary for sending events to client
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

    yield ("data: %s\n\n" % json.dumps(getusers("", room))).encode()
    # this time, add a timeout so it doesn't hang forever
    rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip(), socket_timeout=30)
    start_time = time()
    LIMIT = 60
    try:
        with rds.monitor() as m:
            for command in m.listen():
                if 'wsgi.errors' in environ and environ['wsgi.errors'].closed:
                    return ret_200(start_response, "data: %s\n\n" % json.dumps(getusers("", room)))
                cmd = command['command'].lower()
                if (time() - start_time) > LIMIT:
                    return ret_200(start_response, "data: %s\n\n" % json.dumps(getusers("", room)))
                if "del room"+room.lower() in cmd or "set room"+room.lower() in cmd:
                    start_time = time()
                    try:
                        yield ("data: %s\n\n" % json.dumps(getusers("", room))).encode()
                        #sys.stderr.write("DEBUG sseupdate: " + str(environ['REMOTE_USER']) + "," + str(room) + "," + str(os.getpid()) + "\n")
                    except:
                        try:
                            sys.exit(0)
                        except:
                            os._exit(0)
    except: # includes redis.TimeoutError
        try:
            yield ("data: %s\n\n" % json.dumps(getusers("", room))).encode()
        except:
            try:
                sys.exit(0)
            except:
                os._exit(0)
    yield ("data: %s\n\n" % json.dumps(getusers("", room))).encode()
                
MIDDLEWARES = [ ]
app = reduce(lambda h, m: m(h), MIDDLEWARES, application)
