from time import sleep
import os, sys
import shutil

# grab config based on IP and init all variables
# NOTE: this import-time value is only a fallback -- see set_private_path()
# below and lib/privatepath.py.
try:
    private = '/web/groups/' + os.environ['USER'] + '/private/queup/'
except:
    private = os.environ['CONTEXT_DOCUMENT_ROOT'].replace('public_html', 'private') + 'queup/'

def set_private_path(path):
    """Point this module at the private dir of the tree serving this request."""
    global private
    private = path

def acquireLock(path):
    t = 0
    while t < 5 and os.path.exists(path + ".lck"):
        sleep(1)
        t += 1
    if t == 5:
        raise Exception("lockdir " + path + ".lck" + " not removed.")
    try:
        os.mkdir(path + ".lck")
    except:
        raise Exception("Unable to create lockdir")
    return path + ".lck"

def releaseLock(lock):
    if os.path.exists(lock):
        try:
            shutil.rmtree(lock)
        except:
            raise Exception("Unable to remove lockdir")

def lockAndWriteLog(room, data):
    global private
    lock = acquireLock(private + "/logs/" + room + ".log")
    with open(private + "/logs/" + room + ".log", "a+") as f:
        f.write(data + "\n")
    releaseLock(lock)

class Lock:
    def __init__(self, lockdir):
        self.lockdir = lockdir
        self.lock = None
    def __enter__(self):
        self.lock = acquireLock(self.lockdir)
        return self.lock
    def __exit__(self, type, value, traceback):
        releaseLock(self.lock)