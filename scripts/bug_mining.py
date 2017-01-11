'''
This script assumes you have already done src-to-src transformation with
lavaTool to add taint and attack point queries to a program, AND managed to
get it to compile.  The script 

Only two inputs to the script.

First is a json project file.  The set of asserts below 
indicate the required json fields and their meaning.

Second is input file you want to run, under panda, to get taint info.  
'''

from __future__ import print_function

import os
import sys
import tempfile
import subprocess32
from subprocess32 import PIPE
import shutil
import time
import pipes
import json
from colorama import Fore, Style
from pexpect import spawn
import pexpect
from os.path import dirname, abspath, join
import psutil

from lava import LavaDatabase, Dua, Bug, AttackPoint

debug = True
qemu_use_rr = False

start_time = 0

def tick():
    global start_time
    start_time = time.time()

def tock():
    global start_time
    return time.time() - start_time


def dprint(msg):
    if debug:
        print(msg)

def progress(msg):
    print('')
    print(Fore.GREEN + '[bug_mining.py] ' + Fore.RESET + Style.BRIGHT + msg + Style.RESET_ALL)

if len(sys.argv) < 3:
    print("Usage: python project.json inputfile", file=sys.stderr)
    sys.exit(1)


tick()

project_file = abspath(sys.argv[1])
input_file = abspath(sys.argv[2])

print("bug_mining.py %s %s" % (project_file, input_file))

input_file_base = os.path.basename(input_file)
project = json.load(open(project_file, "r"))


# *** Required json fields 
# path to qemu exec (correct guest)
assert 'qemu' in project
# name of snapshot from which to revert which will be booted & logged in as root?
assert 'snapshot' in project
# same directory as in add_queries.sh, under which will be the build
assert 'directory' in project
# command line to run the target program (already instrumented with taint and attack queries)
assert 'command' in project
# path to guest qcow
assert 'qcow' in project
# name of project 
assert 'name' in project
# path to tarfile for target (original source)
assert 'tarfile' in project
# if needed, what to set LD_LIBRARY_PATH to
assert 'library_path' in project
# namespace in db for prospective bugs
assert 'db' in project
# process name
#assert 'proc_name' in project
proc_name = os.path.basename(project['command'].split()[0])

chaff = False
if 'chaff' in project:
    chaff = True

assert 'panda_os_string' in project

panda_os_string = project['panda_os_string']


lavadir = dirname(dirname(abspath(sys.argv[0])))

progress("Entering {}.".format(project['directory']))

os.chdir(os.path.join(project['directory'], project['name']))

tar_files = subprocess32.check_output(['tar', 'tf', project['tarfile']])
sourcedir = tar_files.splitlines()[0].split(os.path.sep)[0]
sourcedir = abspath(sourcedir)

print('')
isoname = '{}-{}.iso'.format(sourcedir, input_file_base)
progress("Creating ISO {}...".format(isoname))
installdir = join(sourcedir, 'lava-install')
shutil.copy(input_file, join(installdir, input_file_base))
subprocess32.check_call(['genisoimage', '-RJ', '-max-iso9660-filenames',
    '-o', isoname, installdir])
try: os.mkdir('inputs')
except: pass
shutil.copy(input_file, 'inputs/')
os.unlink(join(installdir, input_file_base))

tempdir = tempfile.mkdtemp()

# Find open VNC port.
connections = psutil.net_connections(kind='tcp')
vnc_ports = filter(lambda x : x >= 5900 and x < 6000, [c.laddr[1] for c in connections])
vnc_displays = set([p - 5900 for p in vnc_ports])
new_vnc_display = None
for i in range(10, 100):
    if i not in vnc_displays:
        new_vnc_display = i
        break
if new_vnc_display == None:
    progress("Couldn't find VNC display!")
    sys.exit(1)

monitor_path = os.path.join(tempdir, 'monitor')
serial_path = os.path.join(tempdir, 'serial')
qemu_args = [project['qemu'], project['qcow'], '-loadvm', project['snapshot'],
        '-monitor', 'unix:' + monitor_path + ',server,nowait',
        '-serial', 'unix:' + serial_path + ',server,nowait',
        '-vnc', ':' + str(new_vnc_display)]
if qemu_use_rr:
    qemu_args = ['rr', 'record'] + qemu_args

print('')
progress("Running qemu with args:")
print(subprocess32.list2cmdline(qemu_args))
print('')

os.mkfifo(monitor_path)
os.mkfifo(serial_path)
monitor_wait = subprocess32.Popen(['inotifywait', '-e', 'open', monitor_path],
                                  stdout=PIPE, stderr=PIPE)
serial_wait = subprocess32.Popen(['inotifywait', '-e', 'open', serial_path],
                                 stdout=PIPE, stderr=PIPE)
qemu = subprocess32.Popen(qemu_args, stderr=subprocess32.STDOUT)

try:
    monitor_wait.communicate(timeout=15)
    serial_wait.communicate(timeout=15)
except subprocess32.TimeoutExpired: pass

monitor = spawn("socat", ["stdin", "unix-connect:" + monitor_path])
monitor.logfile = open(os.path.join(tempdir, 'monitor.txt'), 'w')
console = spawn("socat", ["stdin", "unix-connect:" + serial_path])
console.logfile = open(os.path.join(tempdir, 'console.txt'), 'w')

def run_monitor(cmd):
    print(Style.BRIGHT + "(qemu) " + cmd + Style.RESET_ALL)
    monitor.sendline(cmd)
    monitor.expect_exact("(qemu)")
    print(monitor.before.partition("\r\n")[2])

def run_console(cmd, expectation="root@debian-i386:~", timeout=-1):
    print(Style.BRIGHT + "root@debian-i386:~# " + cmd + Style.RESET_ALL)
    console.sendline(cmd)
    try:
        console.expect_exact(expectation, timeout=timeout)
    except pexpect.TIMEOUT:
        print(console.before)
        raise

    print(console.before.partition("\n")[2])

# Make sure monitor/console are in right state.
monitor.expect_exact("(qemu)")
console.sendline("")
console.expect_exact("root@debian-i386:~#")

progress("Inserting CD...")
run_monitor("change ide1-cd0 {}".format(isoname))

run_console("mkdir -p {}".format(installdir))

# Make sure cdrom didn't automount
# Make sure guest path mirrors host path
run_console("while ! mount /dev/cdrom '{}'; do sleep 0.3; umount /dev/cdrom; done".format(installdir))

# Use the ISO name as the replay name.
progress("Beginning recording queries...")
run_monitor("begin_record {}".format(isoname))

progress("Running command inside guest...")
input_file_guest = join(installdir, input_file_base)
expectation = project['expect'] if 'expect' in project else "root@debian-i386:~"
env = project['env'] if 'env' in project else {}
env['LD_LIBRARY_PATH'] = project['library_path'].format(install_dir=installdir)
env_string = " ".join(["{}={}".format(pipes.quote(k), pipes.quote(env[k])) for k in env])
command = project['command'].format(
    install_dir=installdir, input_file=input_file_guest)
run_console(env_string + " " + command, expectation)

progress("Ending recording...")
run_monitor("end_record")

monitor.sendline("quit")
monitor.close()
console.close()
try:
    qemu.wait(timeout=3)
except subprocess32.TimeoutExpired:
    qemu.terminate()
shutil.rmtree(tempdir)

record_time = tock()
print("panda record complete %.2f seconds" % record_time)
sys.stdout.flush()

tick()
print('')
progress("Starting first and only replay, tainting on file open...")


pandalog = "%s/%s/queries-%s.plog" % (project['directory'], project['name'], os.path.basename(isoname))
print("pandalog = [%s] " % pandalog)

pri_taint_args = "hypercall"
if chaff:
    pri_taint_args += ",log_untainted"

qemu_args = [project['qemu'], '-replay', isoname,
        '-pandalog', pandalog,
        '-os', panda_os_string,
        '-panda', 'pri',
        '-panda', 'pri_dwarf:proc=%s,g_debugpath=%s,h_debugpath=%s' % (proc_name, installdir, installdir),
        '-panda', 'pri_taint:' + pri_taint_args,
        '-panda', 'taint2:no_tp',
        '-panda', 'tainted_branch',
        '-panda', 'file_taint:pos,enable_taint_on_open=true,filename={}'.format(
            'stdin' if 'use_stdin' in project else input_file_guest)]

dprint ("qemu args: [%s]" % (" ".join(qemu_args)))
try:
    subprocess32.check_call(qemu_args, stderr=subprocess32.STDOUT)
except subprocess32.CalledProcessError:
    if qemu_use_rr:
        qemu_args = ['rr', 'record', project['qemu'], '-replay', isoname]
        subprocess32.run(qemu_args)
    else: raise

replay_time = tock()
print("taint analysis complete %.2f seconds" % replay_time)
sys.stdout.flush()

tick()

progress("Trying to create database {}...".format(project['name']))
createdb_args = ['createdb', '-U', 'postgres', project['db']]
createdb_result = subprocess32.call(createdb_args, stdout=sys.stdout, stderr=sys.stderr)

print('')
if createdb_result == 0: # Created new DB; now populate
    progress("Database created. Initializing...")
    psql_args = ['psql', '-U', 'postgres', '-d', project['db'],
                 '-f', join(join(lavadir, 'include'), 'lava.sql')]
    dprint ("psql invocation: [%s]" % (" ".join(psql_args)))
    subprocess32.check_call(psql_args, stdout=sys.stdout, stderr=sys.stderr)
else:
    progress("Database already exists.")

print('')
progress("Calling the FBI on queries.plog...")
fbi_args = [join(lavadir, 'fbi', 'fbi'), project_file, sourcedir, pandalog, input_file_base]
dprint ("fbi invocation: [%s]" % (" ".join(fbi_args)))
subprocess32.check_call(fbi_args, stdout=sys.stdout, stderr=sys.stderr)

print('')
progress("Found Bugs, Injectable!!")

fib_time = tock()
print("fib complete %.2f seconds" % fib_time)
sys.stdout.flush()

db = LavaDatabase(project)

print("total dua:", db.session.query(Dua).count())
print("total atp:", db.session.query(AttackPoint).count())
print("total bug:", db.session.query(Bug).count())


