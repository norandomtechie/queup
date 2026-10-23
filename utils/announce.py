#! /usr/bin/env python3
"""Announce a message to QueUp rooms, as a staff broadcast: every open tab shows it as an alert.

Run on charlotte (where the Redis socket is), e.g.
    python3 announce.py "QueUp will be down for maintenance at 9pm."
    python3 announce.py --tree queup-dev --rooms TESTS "testing"

By default it goes to rooms used in the last 12 hours (a room's log was written to), after a confirmation.
The alert reads 'MESSAGE' from <your username>., and each room's log records it as rbcast.
"""
import argparse
import getpass
import json
import os
import sys
import time
import uuid

GROUP = "/web/groups/" + os.environ.get("USER", getpass.getuser())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("message")
    ap.add_argument("--tree", default="queup", help="deployment: queup (default) or queup-dev")
    pick = ap.add_mutually_exclusive_group()
    pick.add_argument("--rooms", help="comma-separated room codes, instead of recently used rooms")
    pick.add_argument("--all", action="store_true", help="every room in the database")
    ap.add_argument("--hours", type=float, default=12, help="what counts as recently used (default 12)")
    ap.add_argument("--dry-run", action="store_true", help="list the rooms, change nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    private = GROUP + "/private/" + args.tree + "/"
    web = GROUP + "/public_html/" + args.tree
    if not os.path.isfile(private + "redis_socket") or not os.path.isdir(web + "/lib"):
        sys.exit("no such deployment: " + args.tree)
    # the deployment's own code, so rooms are written exactly as roomd writes them
    sys.path.insert(0, web)
    import lib.methods
    import lib.lock
    import lib.relay
    lib.methods.set_private_path(private)
    lib.lock.set_private_path(private)
    rds = lib.methods.rds

    existing = sorted(k.decode()[4:] for k in rds.scan_iter("room?????"))
    if args.rooms:
        rooms = [r.strip().upper() for r in args.rooms.split(",") if r.strip()]
        missing = [r for r in rooms if r not in existing]
        if missing:
            sys.exit("no such room: " + ", ".join(missing))
    elif args.all:
        rooms = existing
    else:
        cutoff = time.time() - args.hours * 3600
        rooms = [r for r in existing if os.path.exists(private + "logs/" + r + ".log")
                 and os.path.getmtime(private + "logs/" + r + ".log") >= cutoff]
    if not rooms:
        sys.exit("no rooms to announce to")

    user = getpass.getuser()
    print("%s: %d room(s): %s" % (args.tree, len(rooms), " ".join(rooms)))
    print("alert: BROADCAST RECEIVED: '%s' from %s." % (args.message, user))
    if args.dry_run:
        return
    if not args.yes and input("send? [y/N] ").strip().lower() != "y":
        sys.exit("not sent")

    def mutate(rds_room, pipe):
        # same format as lib.methods.sendbroadcast, written atomically so a queue change made meanwhile isn't lost
        rds_room["broadcast"] = {"message": "'" + args.message + "' from " + user + ".", "id": str(uuid.uuid4())}
        return True, None

    for room in rooms:
        try:
            lib.methods.update_room_atomically(room, mutate)
        except Exception as e:
            print("  %s: failed: %s" % (room, e))
            continue
        lib.lock.lockAndWriteLog(room, ",".join([str(time.time()), user, "rbcast", room]))
        # tabs on SSE see the write itself; tabs on the relay need it pushed
        relayed = lib.relay.publish(private, room, lib.relay.room_payload(rds, room))
        print("  %s: sent%s" % (room, "" if relayed else " (relay not reached: its tabs see it at their next re-check)"))


if __name__ == "__main__":
    main()
