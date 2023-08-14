#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, sys, codecs

username = os.environ['REMOTE_USER']

# enables unicode printing
sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())

# load the HTML
print ("Content-Type: text/html\r\n")
with io.open('index.html', 'r', encoding='utf8') as file:
    # fill username
    data = file.read().replace("--username--", username)
    # send HTML
    print(data)