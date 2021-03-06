
#!/usr/env/python

## Import General Tools
import os
import sys
import re

import argparse
import atexit
import datetime
import getpass
import logging
import math
import pathlib 
import platform
import socket
import subprocess
import telnetlib
import threading
import time
import traceback
import warnings

import yaml

import paramiko


import soundplay

__version__ = '0.1'


class VNCSession(object):
    '''An object to contain information about a VNC session.
    '''
    def __init__(self, name=None, display=None, desktop=None, user=None, pid=None):
        if name is None and display is not None:
            name = ''.join(desktop.split()[1:])
        self.name = name
        self.display = display
        self.desktop = desktop
        self.user = user
        self.pid = pid

    def __str__(self):
        return f"  {self.name:12s} {self.display:5s} {self.desktop:s}"

class LickVncLauncher(object):

    def __init__(self):
        #init vars we need to shutdown app properly
        self.config = None
        self.sound = None
        self.firewall_pass = None
#         self.ssh_threads = None
        self.ports_in_use = {}
        self.vnc_threads  = []
        self.vnc_processes = []
        self.do_authenticate = False
        self.do_forward = True
        self.is_authenticated = False
        self.instrument = None
        self.vncserver = None
        self.is_ssh_key_valid = False
        self.exit = False

        self.servers_to_try = ['shimmy', 'frankfurt']

        #session name consts
        self.SESSION_NAMES = [
            'control0',
            'control1',
            'control2',
            'analysis0',
            'analysis1',
            'analysis2',
            'telanalys',
            'telstatus',
            'status'
        ]

        #default start sessions
        self.DEFAULT_SESSIONS = [
            'Telescope1',
            'Telescope2',
            'Telescope3',
            'Instrument1',
            'Instrument2',
            'Instrument3',
            
        ]

        #NOTE: 'status' session on different server and always on port 1, 
        # so assign localport to constant to avoid conflict
        self.STATUS_PORT       = ':1'
        self.LOCAL_PORT_START  = 5901

        #ssh key constants
        self.SSH_KEY_ACCOUNT = 'user'
        self.SSH_KEY_SERVER  = 'frankfurt.ucolick.org'


    ##-------------------------------------------------------------------------
    ## Start point (main)
    ##-------------------------------------------------------------------------
    def start(self):
    
        #global suppression of paramiko warnings
        #todo: log these?
        warnings.filterwarnings(action='ignore', module='.*paramiko.*')

        ##---------------------------------------------------------------------
        ## Parse command line args and get config
        ##---------------------------------------------------------------------
        self.log.debug("\n***** PROGRAM STARTED *****\nCommand: "+' '.join(sys.argv))
        self.get_args()
        self.get_config()
        self.check_config()

        ##---------------------------------------------------------------------
        ## Log basic system info
        ##---------------------------------------------------------------------
        self.log_system_info()
        self.check_version()

        ##---------------------------------------------------------------------
        ## Authenticate Through Firewall (or Disconnect)
        ##---------------------------------------------------------------------
        #todo: handle blank password error properly
        self.is_authenticated = False
        if self.do_authenticate:
            self.firewall_pass = getpass.getpass(f"Password for firewall authentication: ")
            self.is_authenticated = self.authenticate(self.firewall_pass)
            if not self.is_authenticated:
                self.exit_app('Authentication failure!')

#         if self.args.authonly is True:
#             self.exit_app('Authentication only')


        ##---------------------------------------------------------------------
        ## Determine sessions to open
        ##---------------------------------------------------------------------
#        self.sessions_requested = self.get_sessions_requested(self.args)


        ##---------------------------------------------------------------------
        ## Determine instrument
        ##---------------------------------------------------------------------
        self.instrument, self.tel = self.determine_instrument(self.args.account)
        if not self.instrument: 
            self.exit_app(f'Invalid instrument account: "{self.args.account}"')


        ##---------------------------------------------------------------------
        ## Validate ssh key or use alt method?
        ##---------------------------------------------------------------------
        if self.args.nosshkey is False and self.config.get('nosshkey', None) is None:
            self.validate_ssh_key()
            if not self.is_ssh_key_valid:
                self.log.error("\n\n\tCould not validate SSH key.\n\t"\
                          "Contact mainland_observing@keck.hawaii.edu "\
                          "for other options to connect remotely.\n")
                self.exit_app()
        else:
            self.vnc_password = getpass.getpass(f"Password for user {self.args.account}: ")


        ##---------------------------------------------------------------------
        ## Determine VNC server
        ##---------------------------------------------------------------------
        if self.is_ssh_key_valid:
            self.vncserver = self.get_vnc_server(self.SSH_KEY_ACCOUNT,
                                                 None,
                                                 self.instrument)
        else:
            self.vncserver = self.get_vnc_server(self.args.account,
                                                 self.vnc_password,
                                                 self.instrument)
        if not self.vncserver:
            self.exit_app("Could not determine VNC server.")


        ##---------------------------------------------------------------------
        ## Determine VNC Sessions
        ##---------------------------------------------------------------------
        if self.is_ssh_key_valid:
            # self.engv_account = self.get_engv_account(self.instrument)
            self.sessions_found = self.get_vnc_sessions(self.vncserver,
                                                        self.instrument,
                                                        self.SSH_KEY_ACCOUNT,
                                                        None,
                                                        self.args.account)
        else:
            self.sessions_found = self.get_vnc_sessions(self.vncserver,
                                                        self.instrument,
                                                        self.args.account,
                                                        self.vnc_password,
                                                        self.args.account)
        if self.args.authonly is False and\
                (not self.sessions_found or len(self.sessions_found) == 0):
            self.exit_app('No VNC sessions found')


        ##---------------------------------------------------------------------
        ## Open requested sessions
        ##---------------------------------------------------------------------
        self.calc_window_geometry()
#         self.ssh_threads  = []
        self.ports_in_use = {}
        self.vnc_threads  = []
        self.vnc_processes = []
        for s in self.sessions_found:
            self.start_vnc_session(s.name)


        ##---------------------------------------------------------------------
        ## Open Soundplay
        ##---------------------------------------------------------------------
        sound = None
        if self.args.nosound is False and self.config.get('nosound', False) != True:
            self.start_soundplay()


        ##---------------------------------------------------------------------
        ## Wait for quit signal, then all done
        ##---------------------------------------------------------------------
        atexit.register(self.exit_app, msg="App exit")
        self.prompt_menu()
        self.exit_app()
        #todo: Do we need to call exit here explicitly?  App was not exiting on
        # MacOs but does on linux.


    ##-------------------------------------------------------------------------
    ## Start VNC session
    ##-------------------------------------------------------------------------
    def start_vnc_session(self, session_name):

        self.log.info(f"Opening VNCviewer for '{session_name}'")

#         try:
        #get session data by name
        session = None
        for s in self.sessions_found:
                if s.name == session_name:
                        session = s
                        
        if not session:
            self.log.error(f"No server VNC session found for '{session_name}'.")
            self.print_sessions_found()
            return

        #determine vncserver (only different for "status")
        vncserver = self.vncserver

        #get remote port
        display   = int(session.display)
        port      = int(f"59{display:02d}")

        ## If authenticating, open SSH tunnel for appropriate ports
        if self.do_forward:

            #determine account and password         
            account  = self.SSH_KEY_ACCOUNT if self.is_ssh_key_valid else self.args.account
            password = None if self.is_ssh_key_valid else self.vnc_password

            # determine if there is already a tunnel for this session
            local_port = None
            for p in self.ports_in_use.keys():
                if session_name == self.ports_in_use[p][1]:
                    local_port = p
                    self.log.info(f"Found existing SSH tunnel on port {port}")
                    break

            #open ssh tunnel
            if local_port is None:
                try:
                    local_port = self.open_ssh_tunnel(vncserver, account, password,
                                                    self.ssh_pkey, port, None,
                                                    session_name=session_name)
                except:
                    self.log.error(f"Failed to open SSH tunnel for "
                              f"{account}@{vncserver}:{port}")
                    trace = traceback.format_exc()
                    self.log.debug(trace)
                    return
                
                vncserver = 'localhost'
                
        else:
            local_port = port

        #If vncviewer is not defined, then prompt them to open manually and
        # return now
        if self.config['vncviewer'] in [None, 'None', 'none']:
            self.log.info(f"\nNo VNC viewer application specified")
            self.log.info(f"Open your VNC viewer manually\n")
            return

        #determine geometry
        #NOTE: This doesn't work for mac so only trying for linux
        geometry = ''
        if 'linux' in platform.system().lower():
            i = len(self.vnc_threads) % len(self.geometry)
            geom = self.geometry[i]
            width  = geom[0]
            height = geom[1]
            xpos   = geom[2]
            ypos   = geom[3]
            # if width != None and height != None:
            #     geometry += f'{width}x{height}'
            if xpos != None and ypos != None:
                geometry += f'+{xpos}+{ypos}'

        ## Open vncviewer as separate thread
        self.vnc_threads.append(threading.Thread(target=self.launch_vncviewer,
                                       args=(vncserver, local_port, geometry)))
        self.vnc_threads[-1].start()
        time.sleep(0.05)

    ##-------------------------------------------------------------------------
    ## Get command line args
    ##-------------------------------------------------------------------------
    def get_args(self):
        self.args = create_parser()
        

    ##-------------------------------------------------------------------------
    ## Get Configuration
    ##-------------------------------------------------------------------------
    def get_config(self):

        #define files to try loading in order of pref
        filenames=['local_config.yaml', 'lick_vnc_config.yaml']

        #if config file specified, put that at beginning of list
        filename = self.args.config
        if filename is not None:
            if not pathlib.Path(filename).is_file():
                self.log.error(f'Specified config file "{filename}" does not exist.')
                self.exit_app()
            else:
                filenames.insert(0, filename)

        #find first file that exists
        file = None
        for f in filenames:
            if pathlib.Path(f).is_file():
                file = f
                break
        if not file:
            self.log.error(f'No config files found in list: {filenames}')
            self.exit_app()

        #load config file and make sure it has the info we need
        self.log.info(f'Using config file: {file}')

        # open file a first time just to log the raw contents
        with open(file) as FO:
            contents = FO.read()
#             lines = contents.split('/n')
        self.log.debug(f"Contents of config file: {contents}")

        # open file a second time to properly read config
        with open(file) as FO:
            config = yaml.load(FO, Loader=yaml.FullLoader)

        cstr = "Parsed Configuration:\n"
        for key, c in config.items():
            cstr += f"\t{key} = " + str(c) + "\n"
        self.log.debug(cstr)

        self.config = config


    ##-------------------------------------------------------------------------
    ## Check Configuration
    ##-------------------------------------------------------------------------
    def check_config(self):

        #check for vncviewer
        #NOTE: Ok if not specified, we will tell them to open vncviewer manually
        #todo: check if valid cmd path?
        self.vncviewerCmd = self.config.get('vncviewer', None)
        if not self.vncviewerCmd:
            self.log.warning("Config parameter 'vncviewer' undefined.")
            self.log.warning("You will need to open your vnc viewer manually.\n")

        #checks local port start config
        self.local_port = self.LOCAL_PORT_START
        lps = self.config.get('local_port_start', None)
        if lps: self.local_port = lps

        #check firewall config
        self.do_authenticate = False
        self.firewall_address = self.config.get('firewall_address', None)
        self.firewall_user    = self.config.get('firewall_user',    None)
        self.firewall_port    = self.config.get('firewall_port',    None)
        if self.firewall_address or self.firewall_user or self.firewall_port:
            if self.firewall_address and self.firewall_user and self.firewall_port:
                self.do_authenticate = True
            else:
                self.log.warning("Partial firewall configuration detected in config file:")
                if not self.firewall_address: self.log.warning("firewall_address not set")
                if not self.firewall_user: self.log.warning("firewall_user not set")
                if not self.firewall_port: self.log.warning("firewall_port not set")

        #check ssh_pkeys servers_to try
        self.ssh_pkey = self.config.get('ssh_pkey', None)
        if not self.ssh_pkey:
            self.log.warning("No ssh private key file specified in config file.\n")
        else:
            if not pathlib.Path(self.ssh_pkey).exists():
                self.log.warning(f"SSH private key path does not exist: {self.ssh_pkey}")

        #check default_sessions
        ds = self.config.get('default_sessions', None)
        self.log.debug(f'Default sessions from config file: {ds}')
        if self.args.authonly is True:
            self.log.debug(f'authonly is True, so default sessions set to []')
            ds = []
        if ds is not None: self.DEFAULT_SESSIONS = ds


    ##-------------------------------------------------------------------------
    ## Log basic system info
    ##-------------------------------------------------------------------------
    def log_system_info(self):
        #todo: gethostbyname stopped working after I updated mac. need better method
        try:
            self.log.debug(f'System Info: {os.uname()}')
            hostname = socket.gethostname()
            self.log.debug(f'System hostname: {hostname}')
            # ip = socket.gethostbyname(hostname)
            # self.log.debug(f'System IP Address: {ip}')
            self.log.info(f'Remote Observing Software Version = {__version__}')
        except :
            self.log.error("Unable to log system info.")
            trace = traceback.format_exc()
            self.log.debug(trace)


    ##-------------------------------------------------------------------------
    ## Get sessions to open
    ##-------------------------------------------------------------------------
    def get_sessions_requested(self, args):

        #get sessions to open
        #todo: use const SESSION_NAMES here
        sessions = []

        # create default sessions list if none provided
        if len(sessions) == 0:
            sessions = self.DEFAULT_SESSIONS

        self.log.debug(f'Sessions to open: {sessions}')
        return sessions


    ##-------------------------------------------------------------------------
    ## Print sessions found for instrument
    ##-------------------------------------------------------------------------
    def print_sessions_found(self):

        print(f"\nSessions found for account '{self.args.account}':")
        for s in self.sessions_found:
            print(f"  {s['name']:12s} {s['Display']:5s} {s['Desktop']:s}")


    ##-------------------------------------------------------------------------
    ## List Open Tunnels
    ##-------------------------------------------------------------------------
    def list_tunnels(self):

        if len(self.ports_in_use) == 0:
            print(f"No SSH tunnels opened by this program")
        else:
            print(f"\nSSH tunnels:")
            print(f"  Local Port | Desktop   | Remote Connection")
            for p in self.ports_in_use.keys():
                desktop = self.ports_in_use[p][1]
                remote_connection = self.ports_in_use[p][0]
                print(f"  {p:10d} | {desktop:9s} | {remote_connection:s}")


    ##-------------------------------------------------------------------------
    ## Launch xterm
    ##-------------------------------------------------------------------------
    def launch_xterm(self, command, pw, title):
        cmd = ['xterm', '-hold', '-title', title, '-e', f'"{command}"']
        xterm = subprocess.call(cmd)


    ##-------------------------------------------------------------------------
    ## Open ssh tunnel
    ##-------------------------------------------------------------------------
    def open_ssh_tunnel(self, server, username, password, ssh_pkey, remote_port,
                        local_port=None, session_name='unknown'):

        #get next local port if need be
        #NOTE: Try up to 100 ports beyond
        if not local_port:
            for i in range(0,100):
                if self.is_local_port_in_use(self.local_port): 
                    self.local_port += 1
                    continue
                else:
                    local_port = self.local_port
                    self.local_port += 1
                    break

        #if we can't find an open port, error and return
        if not local_port:
            self.log.error(f"Could not find an open local port for SSH tunnel "
                           f"to {username}@{server}:{remote_port}")
            self.local_port = self.LOCAL_PORT_START
            return False

        #log
        address_and_port = f"{username}@{server}:{remote_port}"
        self.log.info(f"Opening SSH tunnel for {address_and_port} "
                 f"on local port {local_port}.")

        # build the command
        forwarding = f"{local_port}:localhost:{remote_port}"
        command = ['ssh', '-l', username, '-L', forwarding, '-N', '-T', server]
        if ssh_pkey is not None:
            command.append('-i')
            command.append(ssh_pkey)

        self.log.debug('ssh command: ' + ' '.join (command))
        process = subprocess.Popen(command)


        # Having started the process let's make sure it's actually running.
        # First try polling,  then confirm the requested local port is in use.
        # It's a fatal error if either check fails.

        if process.poll() is not None:
            raise RuntimeError('subprocess failed to execute ssh')
        
        checks = 50
        while checks > 0:
            result = self.is_local_port_in_use(local_port)
            if result == True:
                break
            else:
                checks -= 1
                time.sleep(0.1)

        if checks == 0:
            raise RuntimeError('ssh tunnel failed to open after 5 seconds')

        in_use = [address_and_port, session_name, process]
        self.ports_in_use[local_port] = in_use
        
        return local_port


    ##-------------------------------------------------------------------------
    ##-------------------------------------------------------------------------
    def is_local_port_in_use(self, port):
        cmd = f'lsof -i -P -n | grep LISTEN | grep ":{port} (LISTEN)" | grep -v grep'
        self.log.debug(f'Checking for port {port} in use: ' + cmd)
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        data = proc.communicate()[0]
        data = data.decode("utf-8").strip()
        lines = data.split('\n') if data else list()
        if lines:
            self.log.debug(f"Port {port} is in use.")
            return True
        else: 
            return False


    ##-------------------------------------------------------------------------
    ## Launch vncviewer
    ##-------------------------------------------------------------------------
    def launch_vncviewer(self, vncserver, port, geometry=None):

        vncviewercmd   = self.config.get('vncviewer', 'vncviewer')
        vncprefix      = self.config.get('vncprefix', '')
        vncargs        = self.config.get('vncargs', None)

        cmd = [vncviewercmd]
        if vncargs:  
            vncargs = vncargs.split()           
            cmd = cmd + vncargs
        if self.args.viewonly:
            cmd.append('-ViewOnly')
        #todo: make this config on/off so it doesn't break things 
        if geometry: 
            cmd.append(f'-geometry={geometry}')
        cmd.append(f'{vncprefix}{vncserver}:{port:4d}')

        self.log.debug(f"VNC viewer command: {cmd}")
        # proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
        #                         stderr=subprocess.PIPE)
        proc = subprocess.Popen(cmd)

        #append to proc list so we can terminate on app exit
        self.vnc_processes.append(proc)


    ##-------------------------------------------------------------------------
    ## Start soundplay
    ##-------------------------------------------------------------------------
    def start_soundplay(self):

        try:
            #check for existing first and shutdown
            if self.sound:
                self.sound.terminate()

            #config vars
            sound_port  = 9798
            aplay       = self.config.get('aplay', None)
            soundplayer = self.config.get('soundplayer', None)
            vncserver   = self.vncserver

            #Do we need ssh tunnel for this?
            if self.do_authenticate:

                account  = self.SSH_KEY_ACCOUNT if self.is_ssh_key_valid else self.args.account
                password = None if self.is_ssh_key_valid else self.vnc_password
                sound_port = self.open_ssh_tunnel(self.vncserver, account,
                                                  password, self.ssh_pkey,
                                                  sound_port, None)
                if not sound_port:
                    return
                else:
                    vncserver = 'localhost'

            self.sound = soundplay()
            self.sound.connect(self.instrument, vncserver, sound_port,
                               aplay=aplay, player=soundplayer)
        except Exception:
            self.log.error('Unable to start soundplay.  See log for details.')
            trace = traceback.format_exc()
            self.log.debug(trace)



    def play_test_sound(self):
        self.log.warning('Playing of a test sound is not yet implemented')


    ##-------------------------------------------------------------------------
    ## Authenticate through the Keck firewall - needs to be rewritten 
    ##-------------------------------------------------------------------------
    def authenticate(self, authpass):

        #todo: shorten timeout for mistyped password

        self.log.info(f'Authenticating through firewall as:')
        self.log.info(f' {self.firewall_user}@{self.firewall_address}:{self.firewall_port}')

        try:
            with telnetlib.Telnet(self.firewall_address, int(self.firewall_port)) as tn:
                tn.read_until(b"User: ", timeout=5)
                tn.write(f'{self.firewall_user}\n'.encode('ascii'))
                tn.read_until(b"password: ", timeout=5)
                tn.write(f'{authpass}\n'.encode('ascii'))
                tn.read_until(b"Enter your choice: ", timeout=5)
                tn.write('1\n'.encode('ascii'))
                result = tn.read_all().decode('ascii')
                if re.search('User authorized for standard services', result):
                    self.log.info('User authorized for standard services')
                    return True
                else:
                    self.log.error(result)
                    return False
        except:
            self.log.error('Unable to authenticate through firewall')
            trace = traceback.format_exc()
            self.log.debug(trace)
            return False


    ##-------------------------------------------------------------------------
    ## Close Authentication
    ##-------------------------------------------------------------------------
    def close_authentication(self, authpass):

        if not self.is_authenticated:
            return False

        self.log.info('Signing off of firewall authentication')
        try:
            with telnetlib.Telnet(self.firewall_address, int(self.firewall_port)) as tn:
                tn.read_until(b"User: ", timeout=5)
                tn.write(f'{self.firewall_user}\n'.encode('ascii'))
                tn.read_until(b"password: ", timeout=5)
                tn.write(f'{authpass}\n'.encode('ascii'))
                tn.read_until(b"Enter your choice: ", timeout=5)
                tn.write('2\n'.encode('ascii'))
                result = tn.read_all().decode('ascii')
                if re.search('User was signed off from all services', result):
                    self.log.info('User was signed off from all services')
                    return True
                else:
                    self.log.error(result)
                    return False
        except:
            self.log.error('Unable to close firewall authentication!')
            return False


    ##-------------------------------------------------------------------------
    ## Determine Instrument
    ##-------------------------------------------------------------------------
    def determine_instrument(self, account):
        instruments = ('apf','kast', 'nickel')


        telescope = {'apf': 11,
                     'kast':   1,
                     'nickel' : 2,
                    }

        if account.lower() in instruments:
            return instrument, telescope[instrument]

        return None, None


    ##-------------------------------------------------------------------------
    ## Utility function for opening ssh client, executing command and closing
    ##-------------------------------------------------------------------------
    def do_ssh_cmd(self, cmd, server, account, password):
        try:
            output = None
            self.log.debug(f'Trying SSH connect to {server} as {account}:')

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                server, 
                port     = 22, 
                timeout  = 6, 
                key_filename=self.ssh_pkey,
                username = account, 
                password = password)
            self.log.info('  Connected')
        except TimeoutError:
            self.log.error('  Timeout')
        except Exception as e:
            self.log.error('  Failed: ' + str(e))
        else:
            self.log.debug(f'Command: {cmd}')
            stdin, stdout, stderr = client.exec_command(cmd)
            output = stdout.read()
            output = output.decode().strip('\n')
            self.log.debug(f"Output: '{output}'")
        finally:
            client.close()
            return output


    ##-------------------------------------------------------------------------
    ## Validate ssh key on remote vnc server
    ##-------------------------------------------------------------------------
    def validate_ssh_key(self):
        self.log.info(f"Validating ssh key...")

        self.is_ssh_key_valid = False
        cmd = 'whoami'
        data = self.do_ssh_cmd(cmd, self.SSH_KEY_SERVER, self.SSH_KEY_ACCOUNT,
                               None)

        if data == self.SSH_KEY_ACCOUNT:
            self.is_ssh_key_valid = True

        if self.is_ssh_key_valid: self.log.info("  SSH key OK")
        else                    : self.log.error("  SSH key invalid")


    ##-------------------------------------------------------------------------
    ## Get engv account for instrument
    ##-------------------------------------------------------------------------
    def get_engv_account(self, instrument):
        self.log.info(f"Getting engv account for instrument {instrument} ...")

        cmd = f'setenv INSTRUMENT {instrument}; kvncinfo -engineering'
        data = self.do_ssh_cmd(cmd, self.SSH_KEY_SERVER, self.SSH_KEY_ACCOUNT,
                               None)

        engv = None
        if data and ' ' not in data:
            engv = data

        if engv: self.log.debug("engv account is: '{}'")
        else   : self.log.error("Could not get engv account info.")

        return engv


    ##-------------------------------------------------------------------------
    ## Determine VNC Server
    ##-------------------------------------------------------------------------
    def get_vnc_server(self, account, password, instrument):
        self.log.info(f"Determining VNC server for '{account}'...")
        vncserver = None
        for server in self.servers_to_try:
            server += ".ucolick.org"
            cmd = f"vncstatus {instrument}"
            data = self.do_ssh_cmd(cmd, server, account, password)
            # parse data
            if data and len(data) > 3:
                mtch = re.search("Usage",data)
                if not mtch:
                    vncserver = server
                    self.log.info(f"Got VNC server: '{vncserver}'")
                    break

        return vncserver




    ##-------------------------------------------------------------------------
    ## Determine VNC Sessions
    ##-------------------------------------------------------------------------
    def get_vnc_sessions(self, vncserver, instrument, account, password,
                         instr_account):
        self.log.info(f"Connecting to {account}@{vncserver} to get VNC sessions list")

        sessions = []
        cmd = f"vncstatus {instrument}"
        data = self.do_ssh_cmd(cmd, vncserver, account, password)
        
        if data:
            lns = data.split("\n")
            for ln in lns:
                if ln[0] != "#":
                    fields = ln.split('-')
                    display = fields[0].strip()
                    if display == 'Usage':
                        # this should not happen
                        break
                    desktop = fields[1].strip()
                    name = ''.join(desktop.split()[1:]) 
                    s = VNCSession(display=display, desktop=desktop, user=account)
                    sessions.append(s)            
        self.log.debug(f'  Got {len(sessions)} sessions')
        for s in sessions:
            self.log.debug(str(s))

        return sessions


    ##-------------------------------------------------------------------------
    ## Close ssh threads
    ##-------------------------------------------------------------------------
    def close_ssh_thread(self, p):
        if p in self.ports_in_use.keys():
            try:
                remote_connection, desktop, process = self.ports_in_use.pop(p, None)
            except KeyError:
                return
            
            self.log.info(f" Closing SSH tunnel for port {p:d}, {desktop:s} "
                     f"on {remote_connection:s}")
            process.kill()


    def close_ssh_threads(self):
        for p in list(self.ports_in_use.keys()):
            self.close_ssh_thread(p)


    ##-------------------------------------------------------------------------
    ## Calculate vnc windows size and position
    ##-------------------------------------------------------------------------
    def calc_window_geometry(self):

        self.log.debug(f"Calculating VNC window geometry...")

        #get screen dimensions
        #alternate command: xrandr |grep \* | awk '{print $1}'
        cmd = "xdpyinfo | grep dimensions | awk '{print $2}' | awk -Fx '{print $1, $2}'"
        p1 = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
        out = p1.communicate()[0].decode('utf-8')
        screen_width, screen_height = [int(x) for x in out.split()]
        self.log.debug(f"Screen size: {screen_width}x{screen_height}")

        #get num rows and cols 
        #todo: assumming 2x2 always for now; make smarter
        num_win = len(self.sessions_found)
        cols = 2
        rows = 2

        #window coord and size config overrides
        window_positions = self.config.get('window_positions', None)
        window_size = self.config.get('window_size', None)

        #get window width height
        if window_size:
            ww = window_size[0]
            wh = window_size[1]
        else:
            ww = round(screen_width / cols)
            wh = round(screen_height / rows)

        #get x/y coords (assume two rows)
        self.geometry = []
        for row in range(0, rows):
            for col in range(0, cols):
                x = round(col * screen_width/cols)
                y = round(row * screen_height/rows)
                if window_positions:
                    index = len(self.geometry) % len(window_positions)
                    x = window_positions[index][0]
                    y = window_positions[index][1]
                self.geometry.append([ww, wh, x, y])

        self.log.debug('geometry: ' + str(self.geometry))


    ##-------------------------------------------------------------------------
    ## Position vncviewers
    ##-------------------------------------------------------------------------
    def position_vnc_windows(self):

        self.log.info(f"Positioning VNC windows...")

        try:
            #get all x-window processes
            #NOTE: using wmctrl (does not work for Mac)
            #alternate option: xdotool?
            xlines = []
            cmd = ['wmctrl', '-l']
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            while True:
                line = proc.stdout.readline()
                if not line: break
                line = line.rstrip().decode('utf-8')
                self.log.debug(f'wmctrl line: {line}')
                xlines.append(line)

            #reposition each vnc session window
            for i, session in enumerate(self.sessions_found):
                self.log.debug(f'Search xlines for "{session}"')
                win_id = None
                for line in xlines:
                    if session not in line: continue
                    parts = line.split()
                    win_id = parts[0]

                if win_id:
                    index = i % len(self.geometry)
                    geom = self.geometry[index]
                    ww = geom[0]
                    wh = geom[1]
                    wx = geom[2]
                    wy = geom[3]
                    # cmd = ['wmctrl', '-i', '-r', win_id, '-e', f'0,{wx},{wy},{ww},{wh}']
                    cmd = ['wmctrl', '-i', '-r', win_id, '-e',
                           f'0,{wx},{wy},-1,-1']
                    self.log.debug(f"Positioning '{session}' with command: " + ' '.join(cmd))
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                else:
                    self.log.info(f"Could not find window process for VNC session '{session}'")
        except Exception as error:
            self.log.error("Failed to reposition windows.  See log for details.")
            self.log.debug(str(error))


    ##-------------------------------------------------------------------------
    ## Prompt command line menu and wait for quit signal
    ##-------------------------------------------------------------------------
    def prompt_menu(self):

        line_length = 52
        lines = [f"-"*(line_length-2),
                 f"          Lick Remote Observing (v{__version__})",
                 f"                        MENU",
                 f"-"*(line_length-2),
                 f"  l               List sessions available",
                 f"  [session name]  Open VNC session by name",
                 f"  w               Position VNC windows",
                 f"  s               Soundplayer restart",
                 f"  u               Upload log to Lick",
#                  f"|  p               Play a local test sound",
                 f"  t               List local ports in use",
                 f"  c [port]        Close ssh tunnel on local port",
                 f"  v               Check if software is up to date",
                 f"  q               Quit (or Control-C)",
                 f"-"*(line_length-2),
                 ]
        menu = "\n"
        for newline in lines:
            menu += '|' + newline + ' '*(line_length-len(newline)-1) + '|\n'
        menu += "> "

        quit = None
        while quit is None:
            cmd = input(menu).lower()
            cmatch = re.match(r'c (\d+)', cmd)
            if cmd == '':
                pass
            elif cmd == 'q':
                self.log.info(f'Recieved command "{cmd}"')
                quit = True
            elif cmd == 'w':
                self.log.info(f'Recieved command "{cmd}"')
                try:
                    self.position_vnc_windows()
                except:
                    self.log.error("Failed to reposition windows, see log")
                    trace = traceback.format_exc()
                    self.log.debug(trace)
            elif cmd == 'p':
                self.log.info(f'Recieved command "{cmd}"')
                self.play_test_sound()
            elif cmd == 's':
                self.log.info(f'Recieved command "{cmd}"')
                self.start_soundplay()
            elif cmd == 'u':
                self.log.info(f'Recieved command "{cmd}"')
                self.upload_log()
            elif cmd == 'l':
                self.log.info(f'Recieved command "{cmd}"')
                self.print_sessions_found()
            elif cmd == 't':
                self.log.info(f'Recieved command "{cmd}"')
                self.list_tunnels()
            elif cmd == 'v':
                self.log.info(f'Recieved command "{cmd}"')
                self.check_version()
            elif cmatch is not None:
                self.log.info(f'Recieved command "{cmd}"')
                self.close_ssh_thread(int(cmatch.group(1)))
            #elif cmd == 'v': self.validate_ssh_key()
            #elif cmd == 'x': self.kill_vnc_processes()
            elif cmd in self.sessions_found['name']:
                self.log.info(f'Recieved command "{cmd}"')
                self.start_vnc_session(cmd)
            else:
                self.log.info(f'Recieved command "{cmd}"')
                self.log.error(f'Unrecognized command: "{cmd}"')


    ##-------------------------------------------------------------------------
    ## Check for latest version number on GitHub
    ##-------------------------------------------------------------------------
    def check_version(self):
        url = ('https://raw.githubusercontent.com/bpholden/'
               'RemoteObserving/master/lick_vnc_launcher.py')
        try:
            import requests
            from packaging import version
            r = requests.get(url)
            findversion = re.search("__version__ = '(\d.+)'\n", r.text)
            if findversion is not None:
                remote_version = version.parse(findversion.group(1))
                local_version = version.parse(__version__)
            else:
                self.log.warning(f'Unable to determine software version on GitHub')
                return
            if remote_version == local_version:
                self.log.info(f'Your software is up to date (v{__version__})')
            elif remote_version > local_version:
                self.log.info(f'Your software (v{__version__}) is ahead of the released version')
            else:
                self.log.warning(f'Your local software (v{__version__}) is behind '
                                 f'the currently available version '
                                 f'(v{remote_version})')
        except:
            self.log.warning("Unable to verify remote version")

    ##-------------------------------------------------------------------------
    ## Upload log file to Lick
    ##-------------------------------------------------------------------------
    def upload_log(self):
        try:
            user = self.SSH_KEY_ACCOUNT if self.is_ssh_key_valid else self.args.account
            pw = None if self.is_ssh_key_valid else self.vnc_password

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.vncserver,
                port = 22, 
                timeout = 6, 
                key_filename=self.ssh_pkey,
                username = user, 
                password = pw)
            sftp = client.open_sftp()
            self.log.info('  Connected SFTP')

            logfile_handlers = [lh for lh in self.log.handlers if 
                                isinstance(lh, logging.FileHandler)]
            logfile = pathlib.Path(logfile_handlers.pop(0).baseFilename)
            destination = logfile.name
            sftp.put(logfile, destination)
            self.log.info(f'  Uploaded {logfile.name}')
            self.log.info(f'  to {self.args.account}@{self.vncserver}:{destination}')
        except TimeoutError:
            self.log.error('  Timed out trying to upload log file')
        except Exception:
            self.log.error('  Unable to upload logfile: ' + str(e))
            trace = traceback.format_exc()
            self.log.debug(trace)

    ##-------------------------------------------------------------------------
    ## Terminate all vnc processes
    ##-------------------------------------------------------------------------
    def kill_vnc_processes(self, msg=None):

        self.log.info('Terminating all VNC sessions.')
        try:
            #NOTE: poll() value of None means it still exists.
            while self.vnc_processes:
                proc = self.vnc_processes.pop()
                self.log.debug('terminating VNC process: ' + str(proc.args))
                if proc.poll() == None:
                    proc.terminate()

        except:
            self.log.error("Failed to terminate VNC sessions.  See log for details.")
            trace = traceback.format_exc()
            self.log.debug(trace)



    ##-------------------------------------------------------------------------
    ## Common app exit point
    ##-------------------------------------------------------------------------
    def exit_app(self, msg=None):

        #hack for preventing this function from being called twice
        #todo: need to figure out how to use atexit with threads properly
        if self.exit: return

        #todo: Fix app exit so certain clean ups don't cause errors (ie thread not started, etc
        if msg != None: self.log.info(msg)

        #terminate soundplayer
        if self.sound: 
            self.sound.terminate()

        # Close down ssh tunnels and firewall authentication
        if self.do_forward:
            self.close_ssh_threads()
            self.close_authentication(self.firewall_pass)

        #close vnc sessions
        self.kill_vnc_processes()

        self.exit = True
        self.log.info("EXITING APP\n")        
        sys.exit(1)


    ##-------------------------------------------------------------------------
    ## Handle fatal error
    ##-------------------------------------------------------------------------
    def handle_fatal_error(self, error):

        #helpful user error message
        supportEmail = 'mainland_observing@keck.hawaii.edu'
        print("\n****** PROGRAM ERROR ******\n")
        print("Error message: " + str(error) + "\n")
        print("If you need troubleshooting assistance:")
        print(f"* Email {supportEmail}\n")
        #todo: call number, website?

        #Log error if we have a log object (otherwise dump error to stdout) 
        #and call exit_app function
        msg = traceback.format_exc()
        if self.log:
            logfile = self.log.handlers[0].baseFilename
            print(f"* Attach log file at: {logfile}\n")
            self.log.debug(f"\n\n!!!!! PROGRAM ERROR:\n{msg}\n")
        else:
            print(msg)

        self.exit_app()


##-------------------------------------------------------------------------
## Create argument parser
##-------------------------------------------------------------------------
def create_parser():
    ## create a parser object for understanding command-line arguments
    description = (f"Lick VNC Launcher (v{__version__}). This program is used "
                   f"by approved Lick Remote Observing sites to launch VNC "
                   f"sessions for the specified instrument account. For "
                   f"help or information on how to configure the code, please "
                   f"see the included README.md file or email "
                   f"mainland_observing@keck.hawaii.edu")
    parser = argparse.ArgumentParser(description=description)


    ## add flags
    parser.add_argument("--authonly", dest="authonly",
        default=False, action="store_true",
        help="Authenticate through firewall, but do not start VNC sessions.")
    parser.add_argument("--nosound", dest="nosound",
        default=False, action="store_true",
        help="Skip start of soundplay application.")
    parser.add_argument("--viewonly", dest="viewonly",
        default=False, action="store_true",
        help="Open VNC sessions in View Only mode (only for TigerVnC viewer)")
    parser.add_argument("--nosshkey", dest="nosshkey",
        default=False, action="store_true",
        help=argparse.SUPPRESS)

    ## add arguments
    parser.add_argument("account", type=str, nargs='?', default='kast',
                        help="The user account.")

    ## add options
    parser.add_argument("-c", "--config", dest="config", type=str,
        help="Path to local configuration file.")

    #parse
    return parser.parse_args()

##-------------------------------------------------------------------------
## Create logger
##-------------------------------------------------------------------------
def create_logger():

    try:
        ## Create logger object
        log = logging.getLogger('KRO')
        log.setLevel(logging.DEBUG)

        #create log file and log dir if not exist
        ymd = datetime.datetime.utcnow().date().strftime('%Y%m%d')
        pathlib.Path('logs/').mkdir(parents=True, exist_ok=True)

        #file handler (full debug logging)
        logFile = f'logs/lick-remote-log-utc-{ymd}.txt'
        logFileHandler = logging.FileHandler(logFile)
        logFileHandler.setLevel(logging.DEBUG)
        logFormat = logging.Formatter('%(asctime)s UT - %(levelname)s: %(message)s')
        logFormat.converter = time.gmtime
        logFileHandler.setFormatter(logFormat)
        log.addHandler(logFileHandler)

        #stream/console handler (info+ only)
        logConsoleHandler = logging.StreamHandler()
        logConsoleHandler.setLevel(logging.INFO)
        logFormat = logging.Formatter(' %(levelname)8s: %(message)s')
        logFormat.converter = time.gmtime
        logConsoleHandler.setFormatter(logFormat)
        
        log.addHandler(logConsoleHandler)

    except Exception as error:
        print(str(error))
        print(f"ERROR: Unable to create logger at {logFile}")
        print("Make sure you have write access to this directory.\n")
        log.info("EXITING APP\n")        
        sys.exit(1)


##-------------------------------------------------------------------------
## Start from command line
##-------------------------------------------------------------------------
if __name__ == '__main__':

    #catch all exceptions so we can exit gracefully
    try:        
        lvl = LickVncLauncher()
        create_logger()
        lvl.log = logging.getLogger('KRO')
        lvl.start()
    except Exception as error:
        lvl.handle_fatal_error(error)


