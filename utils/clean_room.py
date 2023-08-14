#! /usr/bin/env python3

import time
import sqlite3
from db_read import getrooms

def removeroom(room_db, room):
    conn = sqlite3.connect(room_db)
    cur = conn.cursor()
    cur.execute("DROP TABLE {0}".format("room" + room))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    room_db = "../room.db"
    rooms = [room.replace("room", "") for room in getrooms(room_db).keys()]
    room_time = {}
    log = open("../room.log", "r").read().split("\n")
    for line in log:
        for room in rooms:
            if f"{room},create" in line:
                room_time[room] = float(line.split(",")[0])
                break
    # we should now have a dict of room to their last created time
    # if last created time is more than 24 hours ago, we should remove it
    for room in room_time:
        if time.time() - room_time[room] > 86400:
            removeroom(room_db, room)
            print(f"Removed room {room}.  Last created time was " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(room_time[room])))