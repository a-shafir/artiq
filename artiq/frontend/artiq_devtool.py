#!/usr/bin/env python3

# This script makes the following assumptions:
#  * miniconda is installed remotely at ~/miniconda
#  * misoc and artiq are installed remotely via conda

import sys
import argparse
import logging
import subprocess
import socket
import select
import threading
import os
import shutil

from artiq.tools import verbosity_args, init_logger, logger, SSHClient


def get_argparser():
    parser = argparse.ArgumentParser(description="ARTIQ core device development tool")

    verbosity_args(parser)

    parser.add_argument("-H", "--host", metavar="HOSTNAME",
                        type=str, default="lab.m-labs.hk",
                        help="SSH host where the development board is located")
    parser.add_argument("-D", "--device", metavar="HOSTNAME",
                        type=str, default="kc705.lab.m-labs.hk",
                        help="address or domain corresponding to the development board")
    parser.add_argument("-s", "--serial", metavar="PATH",
                        type=str, default="/dev/ttyUSB_kc705",
                        help="TTY device corresponding to the development board")
    parser.add_argument("-l", "--lockfile", metavar="PATH",
                        type=str, default="/run/boards/kc705",
                        help="The lockfile to be acquired for the duration of the actions")
    parser.add_argument("-w", "--wait", action="store_true",
                        help="Wait for the board to unlock instead of aborting the actions")
    parser.add_argument("-t", "--target", metavar="TARGET",
                        type=str, default="kc705_dds",
                        help="Target to build, one of: "
                             "kc705_dds kc705_drtio_master kc705_drtio_satellite")

    parser.add_argument("actions", metavar="ACTION",
                        type=str, default=[], nargs="+",
                        help="actions to perform, sequence of: "
                             "build reset boot boot+log connect hotswap clean")

    return parser


def main():
    args = get_argparser().parse_args()
    init_logger(args)
    if args.verbose == args.quiet == 0:
        logging.getLogger().setLevel(logging.INFO)

    if args.target == "kc705_dds" or args.target == "kc705_drtio_master":
        firmware = "runtime"
    elif args.target == "kc705_drtio_satellite":
        firmware = "satman"
    else:
        raise NotImplementedError("unknown target {}".format(args.target))

    client = SSHClient(args.host)
    substs = {
        "env":      "bash -c 'export PATH=$HOME/miniconda/bin:$PATH; exec $0 $*' ",
        "serial":   args.serial,
        "firmware": firmware,
    }

    flock_acquired = False
    flock_file = None # GC root
    def lock():
        nonlocal flock_acquired
        nonlocal flock_file

        if not flock_acquired:
            logger.info("Acquiring device lock")
            flock = client.spawn_command("flock --verbose {block} {lockfile} sleep 86400"
                                            .format(block="" if args.wait else "--nonblock",
                                                    lockfile=args.lockfile),
                                         get_pty=True)
            flock_file = flock.makefile('r')
            while not flock_acquired:
                line = flock_file.readline()
                if not line:
                    break
                logger.debug(line.rstrip())
                if line.startswith("flock: executing"):
                    flock_acquired = True
                elif line.startswith("flock: failed"):
                    logger.error("Failed to get lock")
                    sys.exit(1)

    for action in args.actions:
        if action == "build":
            logger.info("Building firmware")
            try:
                subprocess.check_call(["python3",
                                        "-m", "artiq.gateware.targets." + args.target,
                                        "--no-compile-gateware",
                                        "--output-dir",
                                        "/tmp/{target}".format(target=args.target)])
            except subprocess.CalledProcessError:
                logger.error("Build failed")
                sys.exit(1)

        elif action == "clean":
            logger.info("Cleaning build directory")
            target_dir = "/tmp/{target}".format(target=args.target)
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir)

        elif action == "reset":
            lock()

            logger.info("Resetting device")
            client.run_command(
                "{env} artiq_flash start",
                **substs)

        elif action == "boot" or action == "boot+log":
            lock()

            logger.info("Uploading firmware")
            client.get_sftp().put("/tmp/{target}/software/{firmware}/{firmware}.bin"
                                      .format(target=args.target, firmware=firmware),
                                  "{tmp}/{firmware}.bin"
                                      .format(tmp=client.tmp, firmware=firmware))

            logger.info("Booting firmware")
            flterm = client.spawn_command(
                "{env} python3 flterm.py {serial} " +
                "--kernel {tmp}/{firmware}.bin " +
                ("--upload-only" if action == "boot" else "--output-only"),
                **substs)
            artiq_flash = client.spawn_command(
                "{env} artiq_flash start",
                **substs)
            client.drain(flterm)

        elif action == "connect":
            lock()

            transport = client.get_transport()

            def forwarder(local_stream, remote_stream):
                try:
                    while True:
                        r, _, _ = select.select([local_stream, remote_stream], [], [])
                        if local_stream in r:
                            data = local_stream.recv(65535)
                            if data == b"":
                                break
                            remote_stream.sendall(data)
                        if remote_stream in r:
                            data = remote_stream.recv(65535)
                            if data == b"":
                                break
                            local_stream.sendall(data)
                except Exception as err:
                    logger.error("Cannot forward on port %s: %s", port, repr(err))
                local_stream.close()
                remote_stream.close()

            def listener(port):
                listener = socket.socket()
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(('localhost', port))
                listener.listen(8)
                while True:
                    local_stream, peer_addr = listener.accept()
                    logger.info("Accepting %s:%s and opening SSH channel to %s:%s",
                                *peer_addr, args.device, port)
                    try:
                        remote_stream = \
                            transport.open_channel('direct-tcpip', (args.device, port), peer_addr)
                    except Exception:
                        logger.exception("Cannot open channel on port %s", port)
                        continue

                    thread = threading.Thread(target=forwarder, args=(local_stream, remote_stream),
                                              name="forward-{}".format(port), daemon=True)
                    thread.start()

            ports = [1380, 1381, 1382, 1383]
            for port in ports:
                thread = threading.Thread(target=listener, args=(port,),
                                          name="listen-{}".format(port), daemon=True)
                thread.start()

            logger.info("Forwarding ports {} to core device and logs from core device"
                            .format(", ".join(map(str, ports))))
            client.run_command(
                "{env} python3 flterm.py {serial} --output-only",
                **substs)

        elif action == "hotswap":
            logger.info("Hotswapping firmware")
            try:
                subprocess.check_call(["python3",
                    "-m", "artiq.frontend.artiq_coreboot", "hotswap",
                    "/tmp/{target}/software/{firmware}/{firmware}.bin"
                        .format(target=args.target, firmware=firmware)])
            except subprocess.CalledProcessError:
                logger.error("Build failed")
                sys.exit(1)

        else:
            logger.error("Unknown action {}".format(action))
            sys.exit(1)

if __name__ == "__main__":
    main()
