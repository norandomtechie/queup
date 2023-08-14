#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import sys

def print_db(room_db):
    conn = sqlite3.connect(room_db)
    cur = conn.cursor()
    # get all rooms, then get all room data
    print([x for x in cur.execute('SELECT name FROM sqlite_master')])
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_db("../room.db")
    else:
        print_db(sys.argv[1])