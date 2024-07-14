from PyQt5 import QtWidgets, QtCore
from collections import defaultdict

# Pacakage imports
from irrad_control.gui.widgets.arduino_mux_widget import ArduinoMuxWidget
from irrad_control.gui.widgets import NoBackgroundScrollArea

class IrradDevicesTab(QtWidgets.QWidget):
    def __init__(self, setup, parent=None):
        super(IrradDevicesTab, self).__init__(parent)

        self.setup = setup

        self.setLayout(QtWidgets.QVBoxLayout())

        # One tab per server
        self.tabs = QtWidgets.QTabWidget()
        self.layout().addWidget(self.tabs)

        self._beam_down_timer = {}
        self._beam_down = {}

        # Tab widgets
        self.tab_widgets = defaultdict(dict)

        for server in self.setup:
            self._init_tab(server=server)
            self.enable_control(server=server, enable=False)


    def _init_tab(self, server):
        # TODO: make devices selectable

        arduino_mux = ArduinoMuxWidget(server=server)

        # Split tab in quadrants
        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(QtCore.Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter_upper = QtWidgets.QSplitter()
        splitter_upper.setOrientation(QtCore.Qt.Horizontal)
        splitter_upper.setChildrenCollapsible(False)
        splitter_lower = QtWidgets.QSplitter()
        splitter_lower.setOrientation(QtCore.Qt.Horizontal)
        splitter_lower.setChildrenCollapsible(False)

        splitter_upper.addWidget(arduino_mux)

        self.tabs.addTab(NoBackgroundScrollArea(widget=splitter), self.setup[server]['name'])

        # Add to container
        self.tab_widgets[server]['arduino mux'] = arduino_mux

        # Appearance
        self.show()

        splitter_upper.setSizes([self.width(), self.width()])
        splitter_lower.setSizes([self.width(), self.width()])
        splitter.setSizes([self.height(), self.height()])

        # Appearance
        self.show()

        splitter_upper.setSizes([self.width(), self.width()])
        splitter_lower.setSizes([self.width(), self.width()])
        splitter.setSizes([self.height(), self.height()])


    def send_cmd(self, hostname, target, cmd, cmd_data=None):
        self.sendCmd.emit({'hostname': hostname, 'target': target, 'cmd': cmd, 'cmd_data': cmd_data})


    def enable_control(self, server, enable=True):
        pass


    def scan_status(self, server, status='started'):
        pass


    def update_rec_state(self, server, state):
        pass
