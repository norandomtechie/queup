#! /usr/bin/env python3

import sqlite3
import os
import re
from time import sleep, time

class DBConnection:
    def __init__(self, room_db):
        self.conn = sqlite3.connect(room_db)
        self.cur = self.conn.cursor()
    def __enter__(self):
        return [self.conn, self.cur]
    def __exit__(self, type, value, traceback):
        self.conn.commit()
        self.conn.close()

def createroom(room):
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("CREATE TABLE room{0} (queues TEXT)".format(room))

def deleteroom(room):
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("DROP TABLE room{0}".format(room))

def createqueue(queue, room):
    with DBConnection(room_db) as [conn, cur]:
        if f"room{room}" not in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            raise Exception("The room {0} did not exist.".format(room))
        # ensure queue table does not exist already
        # makes sure that anyone on there is no longer there
        if f"room{room}_queue{queue}" in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            deletequeue(queue, room)
        # create the queue table
        cur.execute("CREATE TABLE room{0}_queue{1} (username TEXT KEY, time REAL)".format(room, queue))
        return True

def deletequeue(queue, room):
    with DBConnection(room_db) as [conn, cur]:
        # delete the queue table if it exists
        if f"room{room}_queue{queue}" in [x[0] for x in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            cur.execute("DROP TABLE room{0}_queue{1}".format(room, queue))

def adduser(user, queue, room):
    if queue == "":
        raise Exception("adduser: No queue provided")
    elif room == "":
        raise Exception("adduser: No room provided")
    elif not re.match(r'[A-Z0-9]{5}', room):
        raise Exception("adduser: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("INSERT INTO room{0}_queue{1} (username, time) VALUES (?, ?)".format(room, queue), (user, time()))

def deluser(user, queue, room):
    if queue == "":
        raise Exception("deluser: No queue provided")
    elif room == "":
        raise Exception("deluser: No room provided")
    elif not re.match(r'[A-Z0-9]{5}', room):
        raise Exception("deluser: Room format incorrect: " + room)
    with DBConnection(room_db) as [conn, cur]:
        cur.execute("DELETE FROM room{0}_queue{1} WHERE username == ?".format(room, queue), (user,))

def getrooms():
    with DBConnection(room_db) as [conn, cur]:
        # get all rooms, then get all room data
        with conn:
            roomnames = [row[0] for row in cur.execute('SELECT name FROM sqlite_master') if row[0].startswith("room") and "queue" not in row[0]]
        return roomnames

def getqueues(room):
    with DBConnection(room_db) as [conn, cur]:
        if room == "":
            return [row[0] for row in cur.execute("SELECT name FROM sqlite_master") if re.match(r'room[A-Z0-9]{5}_queue.*', row[0])]
        else:
            # get all queues in room
            return [row[0].replace("room{0}_queue".format(room), "") for row in cur.execute("SELECT name FROM sqlite_master") if row[0].startswith("room{0}_queue".format(room))]

def getusers(queue, room):
    with DBConnection(room_db) as [conn, cur]:
        # we don't need to check if room == "" because getqueues does that for us
        if queue == "":
            queues = getqueues(room)
            all_users = {}
            for _q in queues:
                r = _q.split("_")[0][4:]
                q = _q.split("_")[1][5:]
                if r not in all_users:
                    all_users[r] = {}
                all_users[r][q] = [row for row in cur.execute("SELECT username, time FROM room{0}_queue{1}".format(r, q))]
            return all_users
        else:
            # get all users in queue
            return [row[0] for row in cur.execute("SELECT username, time FROM room{0}_queue{1}".format(room, queue))]

if __name__ == "__main__":
    room_db = "test.db"
    if os.path.exists(room_db):
        os.remove(room_db)
    print("createroom")
    createroom("ABCDE")
    print("rooms: ", getrooms())
    ###############################
    print("\ncreatequeue")
    createqueue("queue1", "ABCDE")
    print("queues in room: ", getqueues("ABCDE"))
    ###############################
    print("\nadduser")
    adduser("user1", "queue1", "ABCDE")
    print("users in queue {0} in room {1}".format("queue1", "ABCDE"), getusers("queue1", "ABCDE"))
    ###############################
    print("\nsleep")
    sleep(1)
    ###############################
    print("\ndeluser")
    deluser("user1", "queue1", "ABCDE")
    print("post-del users in queue {0} in room {1}".format("queue1", "ABCDE"), getusers("queue1", "ABCDE"))
    ###############################
    print("\ndeletequeue")
    deletequeue("queue1", "ABCDE")
    print("post-del queues in room: ", getqueues("ABCDE"))
    ###############################
    print("\ndeleteroom")
    deleteroom("ABCDE")
    print("post-del rooms: ", getrooms())