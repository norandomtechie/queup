#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, sys, codecs, re
import sqlite3

ROOM_RGX = r'^[A-Z0-9]{5}$'

class DBConnection:
    def __init__(self, room_db):
        self.conn = sqlite3.connect(room_db)
        self.cur = self.conn.cursor()
    def __enter__(self):
        return [self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()

def getowners(room_db, room):
    if not os.path.exists(room_db):
        return []
    if not re.match(ROOM_RGX, room):
        raise Exception("getowners: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        allusers = list(cur.execute("SELECT owners FROM room{0}".format(room)))
        if len(allusers) == 0:
            return []
        allusers = allusers[0]
        allusers = allusers[0].split(",")
        return allusers

username = os.environ['REMOTE_USER']
query = os.environ['QUERY_STRING']

parse_qs = {}
for pair in query.split("&"):
    key, value = pair.split("=")
    parse_qs[key] = value

if "@purdue.edu" in username:
    print("Content-Type: text/plain\r\n")
    print("Please log in with your username, not your email.  You may need to clear site settings and try again.")
    sys.exit(0)

# enables unicode printing
sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())

private = os.environ['CONTEXT_DOCUMENT_ROOT'] + "../private/queup/"

# load the HTML
print ("Content-Type: text/html\r\n")
if "room" not in parse_qs:
    print("<h3>Please specify a room.</h3>")
else:
    # check if room is real
    if not os.path.exists(private + "rooms/" + parse_qs["room"] + ".db"):
        print("<h3>Room does not exist.</h3>")
        sys.exit(0)
    # check if user is in owners list
    room_db = private + "rooms/" + parse_qs["room"] + ".db"
    room = parse_qs["room"]
    owners = getowners(room_db, room)
    if username not in owners:
        print("<h3>You are not an owner of this room.  Access has been logged.</h3>")
        sys.exit(0)
    with io.open('index.html', 'r', encoding='utf8') as file:
        # fill username
        data = file.read().replace("--username--", username)
        # fill room
        data = data.replace("--room--", parse_qs["room"])
        # send HTML
        print(data)
