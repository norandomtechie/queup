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
# NOTE: this import-time value is only a fallback -- application() below
# resolves the real per-request path via lib.privatepath, which does not
# depend on this file's own location and so can be imported before that
# path is knowable.
private = '/web/groups/' + os.environ['USER'] + '/private/queup/'

# Import sibling modules relative to this script, not the worker's working directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# Drop cached lib modules so touching this file also reloads lib/
for _name in [n for n in sys.modules if n in ("lib", "alib") or n.startswith(("lib.", "alib."))]:
    del sys.modules[_name]
from lib.lock import *
from lib.methods import *
from lib.ratelimiter import *
from lib.wsgidefs import *
from lib.privatepath import resolve_private_path
from lib.offlinesync import issue_peer_key, apply_offline_batch, MAX_BATCH, get_peer_records, mark_present
import lib.relay
import lib.methods
import lib.lock

def request_intent(query, user):
    """(click time capped at now, "user:opid") sent with queue actions; (None, None) from older clients."""
    try:
        at = min(int(query.get('t', '')) / 1000.0, time())
    except ValueError:
        at = None
    opid = query.get('opid', '')
    # namespaced by the authenticated user, so nobody can mark someone else's operation as decided
    return at, (user + ":" + opid if re.match(PEERID_RGX, opid) else None)

STALE_MSG = "[stale] A newer change was already made, so this older request was not applied."

def room_payload(r, room):
    """What every tab renders: queues, names, timer/broadcast, change times and server time."""
    data = getusers("", room)
    # Latest change time per user|queue, so clients can skip superseded pending actions
    data["watermarks"] = {k.decode(): float(v) for k, v in r.hgetall("qwatermark"+room).items()}
    data["servertime"] = time()
    return data

# Requests that can change what a room looks like; each successful one is pushed to the relay
WRITE_ACTIONS = {"add", "del", "ren", "own", "delown", "setcool", "setsub", "lock", "unlock", "clear",
                 "mark", "setperm", "tgl1q", "timer", "broadcast", "offlinesync"}

def application(environ, start_response):
    statuses = []
    def capture(status, headers, exc_info=None):
        statuses.append(status)
        return start_response(status, headers, exc_info) if exc_info else start_response(status, headers)
    result = handle_request(environ, capture)
    try:
        query = {k: v[0] for k, v in parse_qs(environ.get('QUERY_STRING', '')).items()}
        if statuses and statuses[0].startswith("200") and query.get('action') in WRITE_ACTIONS and 'sseupdate' not in query:
            room = query.get('room', '')
            if re.match(ROOM_RGX, room):
                lib.relay.publish(private, room, room_payload(rds, room) if rds.exists("room"+room) else {})
    except Exception as e:
        sys.stderr.write("relay publish failed: %r\n" % (e,))
    return result

def handle_request(environ, start_response):
    global ip, private, rds

    # Use the private dir of the tree serving this request (see lib/privatepath.py)
    private = resolve_private_path(environ)
    lib.methods.set_private_path(private)
    lib.lock.set_private_path(private)

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
    actions = ['add', 'del', 'chk', 'ren', 'own', 'delown', 'setcool', 'setsub', 'lock', 'unlock', 'clear', 'mark', 'setperm', 'tgl1q', 'timer', 'broadcast', 'peerjoin', 'peerleave', 'offlinesync']
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
            (action in ['add', 'del'] and querychecked),
            # joining the relay: any room participant
            action in ['peerjoin', 'peerleave'],
            # Offline sync: authorized per envelope by signature (see lib/offlinesync.py)
            action == 'offlinesync',
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

    # Relay and offline actions
    will_peerjoin = action == 'peerjoin' and "room"+room in rooms
    will_peerleave = action == 'peerleave' and "room"+room in rooms
    will_offlinesync = action == 'offlinesync' and "room"+room in rooms

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

        # Relay and offline actions (see P2P.md)
        elif will_peerjoin:
            # Any participant; is_owner is computed server-side
            peerid = query.get('peerid', '')
            if not re.match(PEERID_RGX, peerid):
                return ret_400(start_response, "Invalid peerid")
            try:
                # Signing key for offline actions (refused if the peerid belongs to someone else)
                try:
                    version = max(0, min(int(query.get('v', '0')), 10**6))
                except ValueError:
                    version = 0
                signkey = issue_peer_key(room, peerid, user, version)
                if signkey is None:
                    return ret_400(start_response, "peerid already in use")
                mark_present(room, get_peer_records(room, [peerid]), getowners(room))
                # the relay token and server time ride along, so joining is one round trip
                return ret_json(start_response, json.dumps({
                    "status": "joined", "signkey": signkey, "servertime": time(),
                    "relay": lib.relay.mint_token(private, room, user, is_owner)
                }))
            except Exception as e:
                return ret_400(start_response, str(e))
        elif will_peerleave:
            # A closing tab leaves now instead of lingering up to 90s; its signing key stays for sync
            peerid = query.get('peerid', '')
            if not re.match(PEERID_RGX, peerid):
                return ret_400(start_response, "Invalid peerid")
            try:
                rec = get_peer_records(room, [peerid]).get(peerid)
                if rec is None or rec["user"] != user:
                    return ret_400(start_response, "Not your peer")
                rds.zrem("peers"+room, peerid)
                rds.hdel("peermeta"+room, peerid)
                return ret_ok(start_response, "left")
            except Exception as e:
                return ret_400(start_response, str(e))
        elif will_offlinesync:
            try:
                length = int(environ.get('CONTENT_LENGTH', 0) or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 128 * 1024:
                return ret_400(start_response, "Body missing or too large")
            body = environ['wsgi.input'].read(length).decode('utf-8', errors='replace')
            try:
                envelopes = json.loads(body).get('envelopes', [])
                if not isinstance(envelopes, list) or len(envelopes) > MAX_BATCH:
                    raise ValueError("envelopes must be a list of at most %d items" % MAX_BATCH)
            except Exception as e:
                return ret_400(start_response, str(e))
            # Stamped before applying, so later room updates carry a later servertime
            synced_at = time()
            try:
                results, log_lines = apply_offline_batch(room, envelopes, user)
            except redis.RedisError as e:
                return ret_500(start_response, "RedisError: " + str(e))
            for row in log_lines:
                lockAndWriteLog(room, ",".join(row))
            return ret_json(start_response, json.dumps({"results": results, "servertime": synced_at}))
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
                    at, opid = request_intent(query, user)
                    try:
                        clearqueue(queue, room, at, opid)
                    except StaleAction:
                        recordop(room, opid)
                        return ret_ok(start_response, STALE_MSG)
                    except DuplicateAction:
                        return ret_ok(start_response, json.dumps(getusers("", room)))
                    lockAndWriteLog(room, ",".join([str(time()), user, "qclr", room, queue]))
                    return ret_ok(start_response, json.dumps(getusers("", room)))
                elif will_mark:
                    # toggle mark on user
                    at, opid = request_intent(query, user)
                    try:
                        togglemark(username, queue, room, at, opid)
                    except StaleAction:
                        recordop(room, opid)
                        return ret_ok(start_response, STALE_MSG)
                    except DuplicateAction:
                        return ret_ok(start_response, json.dumps(getusers("", room)))
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
            # full=1: the same payload the relay and SSE carry, for tabs re-checking the server
            return ret_ok(start_response, json.dumps(room_payload(rds, room) if query.get('full') == '1' else getusers("", room)))
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
        at, opid = request_intent(query, user)
        if isroomlocked(room) and not is_owner and will_add:
            recordop(room, opid)
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
                        recordop(room, opid)
                        return ret_ok(start_response, "[cooldown] This room only permits you to add yourself to any queue every %d minutes. Please wait %d minutes and %s seconds before adding yourself again." % (cooldown, rem_min, rem_sec))
                if get1q(room):
                    # ensure that user is not in any other queues
                    for q in getqueues(room):
                        if any([user == x[0] for x in getusers(q, room)]):
                            recordop(room, opid)
                            return ret_ok(start_response, "[singlequeue] You are already in a queue.")
                waitdata = query.get('waitdata', '')
                if waitdata != '' and not re.match(WAITDATA_RGX, waitdata):
                    return ret_400(start_response, "Invalid waitdata.")
                addquser(user, waitdata, queue, room, at, opid)
                lockAndWriteLog(room, ",".join([str(time()), user, "uadd", room, queue, waitdata]))
                ret_ok(start_response, "success")
            elif will_del:
                delquser(user, queue, room, at, opid)
                lockAndWriteLog(room, ",".join([str(time()), user, "udel", room, queue]))
                ret_ok(start_response, "success")
            elif staff_del:
                # owner is removing someone from the queue
                delquser(username, queue, room, at, opid)
                lockAndWriteLog(room, ",".join([str(time()), user, "usdel", room, queue, username]))
                ret_ok(start_response, "success")
            else:
                # Nothing to do (already in / not in the queue), but it still fences older pending actions
                subject = username if (is_owner and action == 'del' and username) else user
                if action in ('add', 'del'):
                    fence(room, subject + "|" + queue, at)
                recordop(room, opid)
                return ret_400(start_response, "Invalid action.")
        except StaleAction:
            recordop(room, opid)
            return ret_ok(start_response, STALE_MSG)
        except DuplicateAction:
            return ret_ok(start_response, "success")
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

    # this time, add a timeout so it doesn't hang forever
    rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip(), socket_timeout=30)

    # Room state, read on its own connection
    payload_rds = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())
    def build_payload():
        return room_payload(payload_rds, room)

    yield ("data: %s\n\n" % json.dumps(build_payload())).encode()
    start_time = time()
    LIMIT = 60
    # Keepalive comment at least every 5s, even with no Redis traffic: the load balancer drops
    # connections silent for ~10s, and writing is also how we notice clients that have gone away
    KEEPALIVE = 5
    last_write = time()
    try:
        with rds.monitor() as m:
            while True:
                if 'wsgi.errors' in environ and environ['wsgi.errors'].closed:
                    return ret_200(start_response, "data: %s\n\n" % json.dumps(build_payload()))
                if (time() - start_time) > LIMIT:
                    return ret_200(start_response, "data: %s\n\n" % json.dumps(build_payload()))
                # wait for the next monitored command, but no longer than the keepalive interval
                wait = max(0.1, KEEPALIVE - (time() - last_write))
                if not m.connection.can_read(timeout=wait):
                    last_write = time()
                    yield b": keepalive\n\n"
                    continue
                cmd = m.next_command()['command'].lower()
                is_room_change = "del room"+room.lower() in cmd or "set room"+room.lower() in cmd
                if is_room_change:
                    start_time = time()
                    last_write = start_time
                    try:
                        yield ("data: %s\n\n" % json.dumps(build_payload())).encode()
                        #sys.stderr.write("DEBUG sseupdate: " + str(environ['REMOTE_USER']) + "," + str(room) + "," + str(os.getpid()) + "\n")
                    except GeneratorExit:
                        # Client disconnected: close normally instead of killing the worker
                        raise
                    except:
                        try:
                            sys.exit(0)
                        except:
                            os._exit(0)
                elif time() - last_write >= KEEPALIVE:
                    last_write = time()
                    yield b": keepalive\n\n"
    except GeneratorExit:
        raise
    except: # includes redis.TimeoutError
        try:
            yield ("data: %s\n\n" % json.dumps(build_payload())).encode()
        except GeneratorExit:
            raise
        except:
            try:
                sys.exit(0)
            except:
                os._exit(0)
    yield ("data: %s\n\n" % json.dumps(build_payload())).encode()
                
MIDDLEWARES = [ ]
app = reduce(lambda h, m: m(h), MIDDLEWARES, application)
