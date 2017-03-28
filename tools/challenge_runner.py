#!/usr/bin/env python2

import argparse
import os
import re
import signal
import subprocess
import sys
import time

from common import *
if IS_WINDOWS:
    import win32file

launcher_stdout = 1  # Will be overwritten


def print_to_launcher(s):
    os.write(launcher_stdout, s)


def run_challenge(challenges, chal_timeout, use_signals):
    """ Handler for replay services that connect to the challenge server

    This will setup fds for all challenges according to:
    https://github.com/CyberGrandChallenge/cgc-release-documentation/blob/master/newsletter/ipc.md

    The following state of file descriptors is expected:
        0, 1, 2: client socket
        stdout_fd: This is the stdout of the server launcher. Results are reported here

    Args:
        challenges (list): List of absolute paths to all challenges to launcher
        chal_timeout (int): Maximum time in seconds a challenge is allowed to run for
        use_signals (bool): If True, the challenge runner will use signals to synchronize
                            with the replay services
        stdout_fd (int): fd of the server launcher's stdout
    """
    # Get the test path for logging purposes
    testpath = sys.stdin.readline()

    # Get the seed from cb-replay[-pov]
    # Encoded seed sent as:
    # [record count (4 bytes)] [record type (4 bytes)] [record size (4 bytes)] [seed]
    # TODO: why bother with the encoding? modify cb-replay[-pov] to skip that
    seed = sys.stdin.read(60)[12:].encode('hex')

    # This is the first fd after all of the challenges
    last_fd = 2 * len(challenges) + 3

    try:
        # Move the launcher's stdout away from the challenge fds
        global launcher_stdout
        stdout_fd = int(os.getenv(SERVER_OUT_KEY))

        if IS_WINDOWS:
            # Get a C fd from the HANDLE
            stdout_fd = win32file._open_osfhandle(stdout_fd, os.O_APPEND)
        os.dup2(stdout_fd, last_fd)
        launcher_stdout = last_fd
    except ValueError:
        # If no fd was specified continue as normal
        pass

    # Create all challenge fds
    if len(challenges) > 1:
        # Close fds where the pipes will be placed
        os.closerange(3, last_fd)

        new_fd = 3  # stderr + 1
        for i in xrange(len(challenges)):
            # Create a pipe for every running binary
            rpipe, wpipe = os.pipe()

            # The write end of the pipe needs to be at the lower fd, so it *may* get dup'd over the read end
            # Preemptively dup the read fd here to avoid the issue
            rpipe_tmp = os.dup(rpipe)
            pipe_fds = [wpipe, rpipe_tmp]

            # Duplicate the pipe ends to the correct fds if needed
            for fd in pipe_fds:
                if fd != new_fd:
                    os.dup2(fd, new_fd)
                new_fd += 1

            # Done with the temporary dup
            os.close(rpipe_tmp)

    # Start all challenges
    cb_env = {'seed': seed}
    procs = [subprocess.Popen(c, env=cb_env) for c in challenges]

    # Send a signal to cb-replay to tell it the challenges are ready
    # NOTE: cb-replay has been modified to wait for this
    # This forces cb-replay to wait until all binaries are running,
    # avoiding the race condition where the replay starts too early
    rp_send_sync()

    # Continue until any of the processes die
    try:
        with Timeout(chal_timeout):
            # Wait until any process exits
            while all([proc.poll() is None for proc in procs]):
                time.sleep(0.1)

            # Give the others a chance to exit
            map(lambda p: p.wait(), procs)
    except TimeoutError:
        pass

    # Kill any remaining processes
    for proc in procs:
        if proc.poll() is None:
            terminate(proc)

    # Close all of the ipc pipes
    os.closerange(3, last_fd)

    # If any of the processes crashed, print out crash info
    for proc in procs:
        pid, sig = proc.pid, abs(proc.returncode)
        if sig not in [None, 0, signal.SIGTERM]:
            print_to_launcher('[DEBUG] pid: {}, sig: {}\n'.format(pid, sig))

            # Attempt to get register values
            regs = get_core_dump_regs(pid)
            if regs is not None:
                # If a core dump was generated, report this as a crash
                print_to_launcher('Process generated signal (pid: {}, signal: {}) - {}\n'.format(pid, sig, testpath))

                # Report the register states
                reg_str = ' '.join(['{}:{}'.format(reg, val) for reg, val in regs.iteritems()])
                print_to_launcher('register states - {}\n'.format(reg_str))

    # Final cleanup
    clean_cores(procs)


def get_core_dump_regs(pid):
    """ Read all register values from a core dump
    On OS X, all core dumps are stored as /cores/core.[pid]
    On Linux, the core dump is stored as a 'core' file in the cwd

    Args:
        pid (int): pid of the process that generated the core dump
    Returns:
        (dict): Registers and their values
    """
    # Create a gdb/lldb command to get regs
    if IS_DARWIN:
        cmd = [
            'lldb',
            '--core', '/cores/core.{}'.format(pid),
            '--batch', '--one-line', 'register read'
        ]
    elif IS_LINUX:
        cmd = [
            'gdb',
            '--core', 'core',
            '--batch', '-ex', 'info registers'
        ]

    # Read the registers
    dbg_out = '\n'.join(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate())
    if 'No such file or directory' in dbg_out or "doesn't exist" in dbg_out:
        print_to_launcher('Core dump not found, are they enabled on your system?\n')
        return

    # Parse out registers/values
    regs = {}
    for line in dbg_out.split('\n'):
        # Try to match a register value
        match = re.search(r'([a-z]+)[=\ ]+0x([a-fA-F0-9]+)', line)
        if match is not None:
            regs[match.group(1)] = match.group(2)

    return regs


def clean_cores(procs):
    """ Delete all generated core dumps

    Args:
        procs (list): List of all processes that may have generated core dumps
    """
    if IS_DARWIN:
        map(try_delete, ['/cores/core.{}'.format(p.pid) for p in procs])
    elif IS_LINUX:
        try_delete('core')


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('-t', '--timeout', type=int, default=5,
                        help='The time in seconds that challenges are allowed to run before quitting')
    parser.add_argument('--use-signals', action='store_true',
                        help='Use signals to coordinate starting the challenges with another process')
    parser.add_argument('challenge_paths', nargs='+',
                        help='List of paths to challenge binaries to be started')

    args = parser.parse_args(sys.argv[1:])

    # Run the challenge
    run_challenge(args.challenge_paths, args.timeout, args.use_signals)


if __name__ == '__main__':
    exit(main())
