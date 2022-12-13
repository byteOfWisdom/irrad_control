import sys
import time
import logging
import platform
import zmq
from collections import defaultdict
from email import message_from_string
from pkg_resources import get_distribution, DistributionNotFound
from PyQt5 import QtCore, QtWidgets, QtGui
from threading import Event

# Package imports
from irrad_control.utils.logger import CustomHandler, LoggingStream, log_levels
from irrad_control.utils.worker import QtWorker
from irrad_control.utils.proc_manager import ProcessManager
from irrad_control.gui.widgets import DaqInfoWidget, LoggingWidget
from irrad_control.gui.tabs import IrradSetupTab, IrradControlTab, IrradMonitorTab


PROJECT_NAME = 'Irrad Control'
GUI_AUTHORS = 'Pascal Wolf'
MINIMUM_RESOLUTION = (1366, 768)

try:
    pkgInfo = get_distribution('irrad_control').get_metadata('PKG-INFO')
    AUTHORS = message_from_string(pkgInfo)['Author']
except (DistributionNotFound, KeyError):
    AUTHORS = 'Not defined'


class IrradGUI(QtWidgets.QMainWindow):
    """Inits the main window of the irrad_control software."""

    # PyQt signals
    data_received = QtCore.pyqtSignal(dict)  # Signal for data
    reply_received = QtCore.pyqtSignal(dict)  # Signal for reply
    log_received = QtCore.pyqtSignal(dict)  # Signal for log

    def __init__(self, parent=None):
        super(IrradGUI, self).__init__(parent)

        # Setup dict of the irradiation; is set when setup tab is completed
        self.setup = None
        
        # Needed in order to stop helper threads
        self.stop_recv_data = Event()
        self.stop_recv_log = Event()
        
        # ZMQ context; THIS IS THREADSAFE! SOCKETS ARE NOT!
        # EACH SOCKET NEEDS TO BE CREATED WITHIN ITS RESPECTIVE THREAD/PROCESS!
        self.context = zmq.Context()
        
        # QThreadPool manages GUI threads on its own; every runnable started via start(runnable) is auto-deleted after.
        self.threadpool = QtCore.QThreadPool()

        # Class to manage the server, interpreter and additional subprocesses
        self.proc_mngr = ProcessManager()

        # Keep track of send commands in order to wait for their response
        self._cmd_id = 0
        self._cmd_reply = defaultdict(list)
        self._try_close = False
        self._log_close = False

        # Keep track of successfully started daq processes
        self._started_daq_proc_hostnames = []
        
        # Connect signals
        self.data_received.connect(lambda data: self.handle_data(data))
        self.reply_received.connect(lambda reply: self.handle_reply(reply))
        self.log_received.connect(lambda log: self.handle_log(log))

        # Tab widgets
        self.setup_tab = None
        self.control_tab = None
        self.monitor_tab = None

        # Init user interface
        self._init_ui()
        self._init_logging()

        # Timer starting when application should be closed
        self.close_timer = QtCore.QTimer()
        self.close_timer.timeout.connect(self.close)
        
    def _init_ui(self):
        """
        Initializes the user interface and displays "Hello"-message
        """

        # Main window settings
        self.setWindowTitle(PROJECT_NAME)
        self.screen = QtWidgets.QDesktopWidget().screenGeometry()
        self.setMinimumSize(MINIMUM_RESOLUTION[0], MINIMUM_RESOLUTION[1])
        self.resize(self.screen.width(), self.screen.height())
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        # Create main layout
        self.main_widget = QtWidgets.QWidget()
        self.main_layout = QtWidgets.QVBoxLayout(self.main_widget)
        self.setCentralWidget(self.main_widget)

        # Add QTabWidget for tab_widget
        self.tabs = QtWidgets.QTabWidget()

        # Main splitter
        self.main_splitter = QtWidgets.QSplitter()
        self.main_splitter.setOrientation(QtCore.Qt.Vertical)
        self.main_splitter.setChildrenCollapsible(False)

        # Sub splitter for log and displaying raw data as it comes in
        self.sub_splitter = QtWidgets.QSplitter()
        self.sub_splitter.setOrientation(QtCore.Qt.Horizontal)
        self.sub_splitter.setChildrenCollapsible(False)

        # Add to main layout
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.addWidget(self.sub_splitter)
        self.main_layout.addWidget(self.main_splitter)

        # Init widgets and add to main windowScatterPlotItem
        self._init_menu()
        self._init_tabs()
        self._init_log_dock()
        
        self.sub_splitter.setSizes([int(1. / 3. * self.width()), int(2. / 3. * self.width())])
        self.main_splitter.setSizes([int(0.8 * self.height()), int(0.2 * self.height())])
        
    def _init_menu(self):
        """Initialize the menu bar of the IrradControlWin"""

        self.file_menu = QtWidgets.QMenu('&File', self)
        self.file_menu.addAction('&Quit', self.file_quit, QtCore.Qt.CTRL + QtCore.Qt.Key_Q)
        self.menuBar().addMenu(self.file_menu)

        self.settings_menu = QtWidgets.QMenu('&Settings', self)
        self.settings_menu.addAction('&Connections')
        self.settings_menu.addAction('&Data path')
        self.menuBar().addMenu(self.settings_menu)

        self.appearance_menu = QtWidgets.QMenu('&Appearance', self)
        self.appearance_menu.setToolTipsVisible(True)
        self.appearance_menu.addAction('&Show/hide log', self.handle_log_ui, QtCore.Qt.CTRL + QtCore.Qt.Key_L)
        self.menuBar().addMenu(self.appearance_menu)

    def _init_tabs(self):
        """
        Initializes the tabs for the control window
        """

        # Add tab_widget and widgets for the different analysis steps
        self.tab_order = ('Setup', 'Control', 'Monitor')

        # Store tabs
        tw = {}

        # Initialize each tab
        for name in self.tab_order:

            if name == 'Setup':
                self.setup_tab = IrradSetupTab(parent=self)
                self.setup_tab.session_setup.setup_widgets['session'].widgets['logging_combo'].currentTextChanged.connect(lambda lvl: self.log_widget.change_level(lvl))
                self.setup_tab.setupCompleted.connect(lambda setup: self._init_setup(setup))
                tw[name] = self.setup_tab
            else:
                tw[name] = QtWidgets.QWidget()

            self.tabs.addTab(tw[name], name)
            self.tabs.setTabEnabled(self.tabs.indexOf(tw[name]), name in ['Setup'])

    def _init_setup(self, setup):

        # Store setup
        self.setup = setup

        # Adjust logging level
        logging.getLogger().setLevel(setup['session']['loglevel'])

        # Update tab widgets accordingly
        self.update_tabs()

        # Init daq info widget
        self._init_daq_dock()

        # Init servers
        self._init_processes()

        # Show a progress dialog so user knows what is happening
        self._init_progress_dialog()

    def _init_progress_dialog(self):

        self.pdiag = QtWidgets.QProgressDialog()
        pdiag_label = QtWidgets.QLabel("Launching application:\n\n->Staring data converter...\n->Configuring {0} server(s)...\n->Starting {0} server(s)...".format(len(self.setup['server'])))
        pdiag_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.pdiag.setLabel(pdiag_label)
        self.pdiag.setRange(0, 0)
        self.pdiag.setMinimumDuration(0)
        self.pdiag.setCancelButton(None)
        self.pdiag.setModal(True)
        self.pdiag.show()

    def _init_log_dock(self):
        """Initializes corresponding log dock"""

        # Widget to display log in, we only want to read log
        self.log_widget = LoggingWidget()
        
        # Dock in which text widget is placed to make it closable without losing log content
        self.log_dock = QtWidgets.QDockWidget()
        self.log_dock.setWidget(self.log_widget)
        self.log_dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea)
        self.log_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable)
        self.log_dock.setWindowTitle('Log')

        # Add to main layout
        self.sub_splitter.addWidget(self.log_dock)
        self.handle_log_ui()

    def _init_daq_dock(self):
        """Initializes corresponding daq info dock"""
        # Make raw data widget
        self.daq_info_widget = DaqInfoWidget(setup=self.setup['server'])

        # Dock in which text widget is placed to make it closable without losing log content
        self.daq_info_dock = QtWidgets.QDockWidget()
        self.daq_info_dock.setWidget(self.daq_info_widget)
        self.daq_info_dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea)
        self.daq_info_dock.setFeatures(QtWidgets.QDockWidget.NoDockWidgetFeatures)
        self.daq_info_dock.setWindowTitle('Data acquisition')

        # Add to main layout
        self.sub_splitter.addWidget(self.daq_info_dock)

    def _init_logging(self, loglevel=logging.INFO):
        """Initializes a custom logging handler and redirects stdout/stderr"""

        # Store loglevel of remote processes; subprocesses send log level and message separately
        self._remote_loglevel = 0
        self._loglevel_names = [lvl for lvl in log_levels if isinstance(lvl, str)]

        # Set logging level
        logging.getLogger().setLevel(loglevel)

        # Create logger instance
        self.logger = CustomHandler(self.main_widget)

        # Add custom logger
        logging.getLogger().addHandler(self.logger)

        # Connect logger signal to logger console
        LoggingStream.stdout().messageWritten.connect(lambda msg: self.log_widget.write_log(msg))
        LoggingStream.stderr().messageWritten.connect(lambda msg: self.log_widget.write_log(msg))
        
        logging.info('Started "irrad_control" on %s' % platform.system())

    def handle_log(self, log_dict):

        if 'level' in log_dict:
            self._remote_loglevel = log_dict['level']

        elif 'log' in log_dict:
            logging.log(level=self._remote_loglevel, msg=log_dict['log'])

    def _init_recv_threads(self):

        # Start receiving log messages from other processes
        self.threadpool.start(QtWorker(func=self.recv_log))

        # Start receiving data from other processes
        self.threadpool.start(QtWorker(func=self.recv_data))

    def _init_processes(self):

        # Loop over all server(s), connect to the server(s) and launch worker for configuration
        server_config_workers = {}
        for server in self.setup['server']:
            # Connect
            self.proc_mngr.connect_to_server(hostname=server, username='pi')

            # Prepare server in QThread on init
            server_config_workers[server] = QtWorker(func=self.proc_mngr.configure_server, hostname=server, branch='release_v2', git_pull=True)

            # Connect workers finish signal to starting process on server
            server_config_workers[server].signals.finished.connect(lambda _server=server: self.start_server(_server))

            # Connect workers exception to log
            self._connect_worker_exception(worker=server_config_workers[server])
            self._connect_worker_close(server_config_workers[server], server)

            # Launch worker on QThread
            self.threadpool.start(server_config_workers[server])

        self.start_interpreter()

    def _started_daq_proc(self, hostname):
        """A DQAProcess has been sucessfully started on *hostname*"""
        
        self._started_daq_proc_hostnames.append(hostname)

        # Enable Control and Monitor tabs for this
        if hostname in self.setup['server']:
            self.control_tab.enable_control(server=hostname)
            self.monitor_tab.enable_monitor(server=hostname)

        # All servers have launched successfully
        if all(s in self._started_daq_proc_hostnames for s in self.setup['server']):
            # The interpreter has also succesfully started
            if 'localhost' in self._started_daq_proc_hostnames:

                # The application has started succesfully
                logging.info("All servers and the converter have started successfully!")
                self.pdiag.setLabelText('Application launched successfully!')
                self.tabs.setCurrentIndex(self.tabs.indexOf(self.monitor_tab))
                QtCore.QTimer.singleShot(1500, self.pdiag.close)

    def collect_proc_infos(self):
        """Run in a separate thread to collect infos of all launched processes"""

        while len(self.proc_mngr.active_pids) != len(self.proc_mngr.launched_procs):

            for proc in self.proc_mngr.launched_procs:

                proc_info = self.proc_mngr.get_irrad_proc_info(proc)

                if proc_info is not None and proc not in self.proc_mngr.active_pids:
                    self.proc_mngr.register_pid(hostname=proc, pid=proc_info['pid'], name=proc_info['name'], ports=proc_info['ports'])

                    # Update setup
                    if proc in self.setup['server']:
                        self.setup['server'][proc]['ports'] = proc_info['ports']
                    else:
                        self.setup['ports'] = proc_info['ports']

            # Wait a second before trying to read something again
            time.sleep(1)

    def send_start_cmd(self):

        for server in self.setup['server']:
            self.send_cmd(hostname=server, target='server', cmd='start', cmd_data={'setup': self.setup, 'server': server})

        self.send_cmd(hostname='localhost', target='interpreter', cmd='start', cmd_data=self.setup)

    def _start_daq_proc(self, hostname, ignore_orphaned=False):

        # Check if there is an already-running irrad process instance; each DAQProcess creates/deletes a hidden pid-file on launch/shutdown
        orphaned_proc = self.proc_mngr.get_irrad_proc_info(hostname=hostname)

        # There is no indication for an orphaned process
        if orphaned_proc is None or ignore_orphaned:

            # We're launching a server
            if hostname in self.proc_mngr.client:
                # Launch server
                self.proc_mngr.start_server_process(hostname=hostname)

            # We're launching an interpreter
            else:
                # Launch interpreter
                self.proc_mngr.start_interpreter_process()

            self.proc_mngr.launched_procs.append(hostname)

            # All servers have been launched; start collecting info
            if all(server in self.proc_mngr.launched_procs for server in self.setup['server']):
                proc_info_worker = QtWorker(func=self.collect_proc_infos)
                proc_info_worker.signals.finished.connect(self._init_recv_threads)
                proc_info_worker.signals.finished.connect(self.send_start_cmd)
                self.threadpool.start(proc_info_worker)

        # There is a pid-file
        else:
            # Check whether a process with the PID in the pid-file is still running
            ps_status = self.proc_mngr.check_process_status(hostname=hostname, pid=orphaned_proc['pid'])

            # The process is running
            if ps_status[hostname]:

                proc_kind = 'server' if hostname in self.proc_mngr.client else 'interpreter'
                pltfrm = 'localhost' if proc_kind == 'interpreter' else self.proc_mngr.server[hostname] + '@' + hostname

                msg = "A {0} process is already running on {1}. Only one {0} process at a time can be run on a host. " \
                      "Do you want to terminate the {0} process and relaunch a new one?" \
                      " Proceeding without terminating the currently running process may lead to faulty behavior".format(proc_kind, pltfrm)

                reply = QtWidgets.QMessageBox.question(self, 'Terminate running {} process and relaunch?'.format(proc_kind),
                                                       msg, QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No)

                if reply == QtWidgets.QMessageBox.Yes:
                    self.proc_mngr.kill_proc(hostname=hostname, pid=orphaned_proc['pid'])
                    self._start_daq_proc(hostname=hostname)  # Try again

            else:
                self._start_daq_proc(hostname=hostname, ignore_orphaned=True)  # Try again

    def start_server(self, server):
        self._start_daq_proc(hostname=server)

    def start_interpreter(self):
        self._start_daq_proc(hostname='localhost')

    def _connect_worker_exception(self, worker):
        worker.signals.exception.connect(lambda e, trace: logging.error("{} on sub-thread: {}".format(type(e).__name__, trace)))

    def _connect_worker_close(self, worker, hostname):
        self._cmd_reply[hostname].append(self._cmd_id)
        for con in [lambda _hostname=hostname, cmd_id=self._cmd_id: self._cmd_reply[_hostname].remove(cmd_id), self._check_close]:
            worker.signals.finished.connect(con)
        self._cmd_id += 1
        
    def _tcp_addr(self, port, ip='*'):
        """Creates string of complete tcp address which sockets can bind to"""
        return 'tcp://{}:{}'.format(ip, port)

    def update_tabs(self):

        current_tab = self.tabs.currentIndex()

        # Create missing tabs
        self.control_tab = IrradControlTab(setup=self.setup['server'], parent=self.tabs)
        self.monitor_tab = IrradMonitorTab(setup=self.setup['server'], parent=self.tabs,
                                           plot_path=self.setup['session']['outfolder'])

        # Connect control tab
        self.control_tab.sendCmd.connect(lambda cmd_dict: self.send_cmd(**cmd_dict))
        self.control_tab.enableDAQRec.connect(lambda server, enable: self.daq_info_widget.record_btns[server].setVisible(enable))
        self.control_tab.enableDAQRec.connect(
            lambda server, enable: self.daq_info_widget.record_btns[server].clicked.connect(
                lambda _, _server=server: self.send_cmd(hostname='localhost',
                                                        target='interpreter',
                                                        cmd='record_data',
                                                        cmd_data=(_server, self.daq_info_widget.record_btns[server].text() == 'Resume')))
            if enable else self.daq_info_widget.record_btns[server].clicked.disconnect())  # Pretty crazy connection. Basically connects or disconnects a button

        # Make temporary dict for updated tabs
        tmp_tw = {'Control': self.control_tab, 'Monitor': self.monitor_tab}

        for tab in self.tab_order:
            if tab in tmp_tw:

                # Remove old tab, insert updated tab at same index and set status
                self.tabs.removeTab(self.tab_order.index(tab))
                self.tabs.insertTab(self.tab_order.index(tab), tmp_tw[tab], tab)

        # Set the tab index to stay at the same tab after replacing old tabs
        self.tabs.setCurrentIndex(current_tab)
    
    def handle_data(self, data):

        server = data['meta']['name']

        # Check whether data is interpreted
        if data['meta']['type'] == 'raw':
            self.daq_info_widget.update_raw_data(data)
            self.monitor_tab.plots[server]['raw_plot'].set_data(meta=data['meta'], data=data['data'])

        # Check whether data is interpreted
        elif data['meta']['type'] == 'beam':
            self.daq_info_widget.update_beam_current(data)
            self.monitor_tab.plots[server]['pos_plot'].set_data(data)
            self.monitor_tab.plots[server]['current_plot'].set_data(meta=data['meta'], data=data['data']['current'])

            if 'frac_h' in data['data']['sey']:
                self.monitor_tab.plots[server]['sem_h_plot'].set_data(data['data']['sey']['frac_h'])
            if 'frac_v' in data['data']['sey']:
                self.monitor_tab.plots[server]['sem_v_plot'].set_data(data['data']['sey']['frac_v'])

            self.control_tab.check_no_beam(server=server, beam_current=data['data']['current']['beam_current'])

        elif data['meta']['type'] == 'hist':
            if 'beam_position_idxs' in data['data']:
                self.monitor_tab.plots[server]['pos_plot'].update_hist(data['data']['beam_position_idxs'])
            if 'sey_horizontal_idx' in data['data']:
                self.monitor_tab.plots[server]['sem_h_plot'].update_hist(data['data']['sey_horizontal_idx'])
            if 'sey_vertical_idx' in data['data']:
                self.monitor_tab.plots[server]['sem_v_plot'].update_hist(data['data']['sey_vertical_idx'])

        elif data['meta']['type'] == 'damage':

            #update_info(scan=data['data']['scan_primary_fluence'][0], unit='p/cm^2')
            pass

        elif data['meta']['type'] == 'scan':

            if data['data']['status'] == 'scan_init':  # Scan is being initialized

                # Disable all record buttons when scan starts
                self.control_tab.tab_widgets[server]['daq'].btn_record.setEnabled(False)
                self.daq_info_widget.record_btns[server].setEnabled(False)

            elif data['data']['status'] in ('scan_start', 'scan_stop'):

                #self.control_tab.update_info(status='Scanning' if data['data']['status'] == 'scan_start' else 'Turning')

                if data['data']['status'] == 'scan_start':
                    # Update control
                    #update_scan_parameters(scan=data['data']['scan'], row=data['data']['row'])
                    #self.control_tab.update_scan_parameters(scan_speed=data['data']['speed'], unit='mm/s')
                    pass

            elif data['data']['status'] == 'scan_finished':
                self.control_tab.scan_status(server=server, status=data['data']['status'])

                # Enable all record buttons when scan is over
                self.control_tab.tab_widgets[server]['daq'].btn_record.setEnabled(True)
                self.daq_info_widget.record_btns[server].setEnabled(True)
                self.control_tab.tab_widgets[server]['scan'].init_after_scan_ui()

                # Check whether data is interpreted
            elif data['data']['status'] == 'interpreted':
                self.monitor_tab.plots[server]['fluence_plot'].set_data(data)
                #self.control_tab.update_info(row=data['data']['row_primary_fluence'][0], unit='p/cm^2')
                #self.control_tab.update_info(nscan=data['data']['eta_n_scans'])

                if data['data']['eta_n_scans'] >= 0:
                    # self.control_tab.update_info(nscan=data['data']['eta_n_scans'])
                    # FIXME: more precise result would be helpful
                    pass

                # Finish the scan programatically, if wanted
                self.control_tab.check_finish(server=server, eta_n_scans=data['data']['eta_n_scans'])

        elif data['meta']['type'] == 'temp_arduino':

            self.monitor_tab.plots[server]['temp_arduino_plot'].set_data(meta=data['meta'], data=data['data'])

        elif data['meta']['type'] == 'temp_daq_board':
            self.monitor_tab.plots[server]['temp_daq_board_plot'].set_data(meta=data['meta'], data=data['data'])

        elif data['meta']['type'] == 'dose_rate':
            self.monitor_tab.plots[server]['dose_rate_plot'].set_data(meta=data['meta'], data=data['data'])
            
        elif data['meta']['type'] == 'axis':
            # Update motorstage positions after every move
            self.control_tab.tab_widgets[server]['motorstage'].update_motorstage_properties(motorstage=data['data']['axis_domain'],
                                                                                            properties={'position': data['data']['position']},
                                                                                            axis=data['data']['axis'])
            

    def send_cmd(self, hostname, target, cmd, cmd_data=None, check_reply=True, timeout=None):
        """Send a command *cmd* to a target *target* running within the server or interpreter process.
        The command can have respective data *cmd_data*."""

        cmd_dict = {'target': target, 'cmd': cmd, 'data': cmd_data}
        cmd_worker = QtWorker(self._send_cmd_get_reply, hostname, cmd_dict, timeout)

        # Make connections
        self._connect_worker_exception(worker=cmd_worker)

        # Keep track of commands
        if check_reply:
            self._connect_worker_close(cmd_worker, hostname)

        # Start
        self.threadpool.start(cmd_worker)

    def _send_cmd_get_reply(self, hostname, cmd_dict, timeout=None):
        """Sending a command to the server / interpreter and waiting for its reply. This runs on a separate QThread due
        to the blocking nature of the recv() method of sockets. *cmd_dict* contains the target, cmd and cmd_data."""

        # Spawn socket to send request to server / interpreter and connect
        req = self.context.socket(zmq.REQ)
        req_port = self.setup['server'][hostname]['ports']['cmd'] if hostname in self.setup['server'] else self.setup['ports']['cmd']

        if timeout:
            req.setsockopt(zmq.RCVTIMEO, int(timeout))
            req.setsockopt(zmq.LINGER, 0)

        req.connect(self._tcp_addr(req_port, hostname))

        # Send command dict and wait for reply
        req.send_json(cmd_dict)

        try:
            reply = req.recv_json()

            # Update reply dict by the servers IP address
            reply['hostname'] = hostname

            # Emit the received reply in pyqt signal and close socket
            self.reply_received.emit(reply)

        except zmq.Again:
            msg = 'Command {} with target {} timed out after {} seconds: no reply from server {}'
            logging.error(msg.format(cmd_dict['cmd'],
                                     cmd_dict['target'],
                                     timeout // 1000,
                                     self.setup['server'][hostname]['name']))
        finally:
            req.close()

    def handle_reply(self, reply_dict):

        reply = reply_dict['reply']
        _type = reply_dict['type']
        sender = reply_dict['sender']
        hostname = reply_dict['hostname']
        reply_data = None if 'data' not in reply_dict else reply_dict['data']

        if _type == 'STANDARD':

            if sender == 'server':

                if reply == 'start':
                    logging.info("Successfully started server on at IP {} with PID {}".format(hostname, reply_data))
                    self._started_daq_proc(hostname=hostname)

                    # Get initial motorstage configuration
                    self.send_cmd(hostname=hostname, target=sender, cmd='motorstages')

                elif reply == 'shutdown':

                    logging.info("Server at {} confirmed shutdown".format(hostname))

                    # FIXME: server does not always send a reply https://github.com/zeromq/libzmq/issues/1264
                    # Try to close
                    self.close()

                elif reply == 'motorstages':
                    for ms, ms_config in reply_data.items():
                        self.control_tab.tab_widgets[hostname]['motorstage'].add_motorstage(motorstage=ms,
                                                                                            positions=ms_config['positions'],
                                                                                            properties=ms_config['props'])

            elif sender == 'IrradDAQBoard':

                if reply == 'set_ifs':
                    cmd_data = {'server': hostname,
                                'ifs': reply_data['callback']['result'],
                                'group': reply_data['call']['kwargs']['group']}
                    self.send_cmd(hostname='localhost', target='interpreter', cmd='update_group_ifs', cmd_data=cmd_data)
                    self.send_cmd(hostname='localhost', target='interpreter', cmd='record_data', cmd_data=(hostname, True))

            elif sender == 'interpreter':

                if reply == 'start':
                    logging.info("Successfully started interpreter on {} with PID {}".format(hostname, reply_data))
                    self._started_daq_proc(hostname=hostname)

                if reply == 'record_data':
                    server, state = reply_data
                    self.daq_info_widget.update_rec_state(server=server, state=state)
                    self.control_tab.update_rec_state(server=server, state=state)

                if reply == 'shutdown':

                    logging.info("Interpreter confirmed shutdown")

                    # Try to close
                    self.close()

            elif sender == '__scan__':

                if reply == 'setup_scan':
                    self.monitor_tab.add_fluence_hist(server=hostname,
                                                      kappa=self.setup['server'][hostname]['daq']['kappa']['nominal'],
                                                      n_rows=reply_data['result']['n_rows'])
                    
                    self.control_tab.scan_status(server=hostname, status='started')
                    self.control_tab.tab_widgets[hostname]['scan'].n_rows = reply_data['result']['n_rows']
                    self.control_tab.tab_widgets[hostname]['scan'].launch_scan()

            # Get motorstage responses
            elif sender in ('ScanStage', 'SetupTableStage', 'ExternalCupStage'):

                if reply in ('set_speed', 'set_range', 'set_accel', 'stop'):
                    # Callback is get_physical_props
                    self.control_tab.tab_widgets[hostname]['motorstage'].update_motorstage_properties(motorstage=sender,
                                                                                                      properties=reply_data['callback']['result'])
                elif reply in ['get_speed', 'get_range', 'get_accel', 'get_position']:
                    prop = reply.split('_')[-1]
                    prop = {prop: reply_data['result']} if not isinstance(reply_data['result'], list) else [{prop: r} for r in reply_data['result']]
                    self.control_tab.tab_widgets[hostname]['motorstage'].update_motorstage_properties(motorstage=sender,
                                                                                                      properties=prop)
                elif reply == 'get_physical_props':
                    self.control_tab.tab_widgets[hostname]['motorstage'].update_motorstage_properties(motorstage=sender,
                                                                                                      properties=reply_data['result'])

                elif reply in ('add_position', 'remove_position'):
                    self.control_tab.tab_widgets[hostname]['motorstage'].motorstage_positions_window.validate(motorstage=sender,
                                                                                                              positions=reply_data['callback']['result'],
                                                                                                              validate=reply.split('_')[0])

            # Debug
            msg = 'Standard {} reply received: {}'.format(sender, reply)
            logging.debug(msg)

        elif _type == 'ERROR':
            msg = '{} error occurred: {}'.format(sender, reply)
            logging.error(msg)
            if self.log_dock.isHidden():
                self.log_dock.setVisible(True)

        else:
            logging.info('Received reply {} from {}'.format(reply, sender))

    def recv_data(self):
        
        # Data subscriber
        data_sub = self.context.socket(zmq.SUB)

        # Loop over servers and connect to their data streams
        for server in self.setup['server']:
            data_sub.connect(self._tcp_addr(self.setup['server'][server]['ports']['data'], ip=server))

        # Connect to interpreter data stream
        data_sub.connect(self._tcp_addr(self.setup['ports']['data'], ip='localhost'))

        data_sub.setsockopt(zmq.SUBSCRIBE, b'')  # specify bytes for Py3
        
        logging.info('Data receiver ready')
        
        while not self.stop_recv_data.is_set():

            self.data_received.emit(data_sub.recv_json())
            
    def recv_log(self):
        
        # Log subscriber
        log_sub = self.context.socket(zmq.SUB)

        # Connect to log messages from remote server and local interpreter process
        # Loop over servers and connect to their data streams
        for server in self.setup['server']:
            log_sub.connect(self._tcp_addr(self.setup['server'][server]['ports']['log'], ip=server))

        # Connect to interpreter data stream
        log_sub.connect(self._tcp_addr(self.setup['ports']['log'], ip='localhost'))

        log_sub.setsockopt(zmq.SUBSCRIBE, b'')  # specify bytes for Py3
        
        logging.info('Log receiver ready')
        
        while not self.stop_recv_log.is_set():
            log = log_sub.recv()
            if log:
                log_dict = {}

                # Py3 compatibility; in Py 3 string is unicode, receiving log via socket will result in bytestring which needs to be decoded first;
                # Py2 has bytes as default; interestinglyy, u'test' == 'test' is True in Py2 (whereas 'test' == b'test' is False in Py3),
                # therefore this will work in Py2 and Py3
                log = log.decode()

                if log.upper() in self._loglevel_names:
                    log_dict['level'] = getattr(logging, log.upper(), None)
                else:
                    log_dict['log'] = log.strip()

                self.log_received.emit(log_dict)

    def handle_messages(self, message, ms=4000):
        """Handles messages from the tabs shown in QMainWindows statusBar"""

        self.statusBar().showMessage(message, ms)

    def handle_log_ui(self):
        """Handle whether log widget is visible or not"""

        if self.log_dock.isVisible():
            self.log_dock.setVisible(False)
        else:
            self.log_dock.setVisible(True)

    def file_quit(self):
        self.close()

    def _check_close(self):
        """Check whether we're waiting for cmd replies in order to close"""
        if self._try_close:
            self.close()

    def _clean_up(self):

        # Stop receiver threads
        self.stop_recv_data.set()
        self.stop_recv_log.set()
        self.close_timer.stop()

        # Store all plots on close; AttributeError when app was not launched fully
        try:
            self.monitor_tab.save_plots()
        except AttributeError:
            pass

        # Wait 1 second for all threads to finish
        self.threadpool.waitForDone(1000)

    def closeEvent(self, event):
        """Catches closing event and invokes customized closing routine"""

        # Repeatedly check if we can close with 0.5 sec interval
        self.close_timer.start(500)

        # Indicate that we want to close
        self._try_close = True

        if any(val for val in self._cmd_reply.values()):

            if not self._log_close:
                for host in self._cmd_reply:
                    if self._cmd_reply[host]:
                        msg = "Waiting for reply from {} with command ID(s): {}".format(host, ', '.join([str(i) for i in self._cmd_reply[host]]))
                        logging.warning(msg)

                logging.warning("{} will be closed after all remaining replies have been received".format(PROJECT_NAME))
                self._log_close = True

            # Ignore closing
            event.ignore()

        # There are subprocesses to shut down
        elif any(self.proc_mngr.active_pids[h][pid]['active'] for h in self.proc_mngr.active_pids for pid in self.proc_mngr.active_pids[h]):

            # If we're here, there's no more processecs; we will launch closing workers, they should not give warning to user
            self._log_close = True

            # Check
            self.proc_mngr.check_active_processes()

            # Loop over all started processes and send shutdown cmd
            for host in self.proc_mngr.active_pids:

                # Shutdown all the servers
                if host in self.setup['server']:
                    logging.info("Shutting down server at {}".format(host))
                    # FIXME: server does not always send a reply https://github.com/zeromq/libzmq/issues/1264
                    self.send_cmd(host, 'server', 'shutdown', check_reply=False)

                # Shutdown interpreter
                if host == 'localhost':
                    logging.info("Shutting down interpreter...")
                    self.send_cmd(host, 'interpreter', 'shutdown', check_reply=False)

            # Ignore closing
            event.ignore()

        else:

            self._clean_up()

            # Close
            event.accept()


def run():
    app = QtWidgets.QApplication(sys.argv)
    font = QtGui.QFont()
    font.setPointSize(11)
    app.setFont(font)
    icg = IrradGUI()
    icg.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    run()
