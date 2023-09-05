#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, sys, codecs

username = os.environ['REMOTE_USER']

if "@purdue.edu" in username:
    print("Content-Type: text/plain\r\n")
    print("Please log in with your username, not your email.  You may need to clear site settings and try again.")
    sys.exit(0)

# enables unicode printing
sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())

# load the HTML
print ("Content-Type: text/html\r\n")
with io.open('index.html', 'r', encoding='utf8') as file:
    # fill username
    data = file.read().replace("--username--", username)
    # send HTML
    print(data)
