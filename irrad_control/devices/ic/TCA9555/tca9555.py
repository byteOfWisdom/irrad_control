import wiringpi as wp
import bitstring as bs
from collections import Iterable


class TCA9555(object):
    """This class implements an interface to the 16-bit IO expander using the I2C-interface of a Raspberry Pi"""

    regs = {
        'input': (0x00, 0x01),
        'output': (0x02, 0x03),
        'polarity': (0x04, 0x05),
        'config': (0x06, 0x07)
    }

    n_gpio = 16

    def __init__(self, address=0x20, config=None):
        """Initialize the connection to the chip"""

        # I2C-bus address; 0x20 (32 in decimal) if all address pins A0=A1=A2 are low
        self.address = address

        # Setup I2C-bus communication using wiringpi library
        self.device_id = wp.wiringPiI2CSetup(self.address)

        # Quick check; if self.device_id == -1 an error occurred
        if self.device_id == -1:
            raise IOError("Failed to establish connection on I2C-bus address {}".format(hex(self.address)))

        self.state = self.get_io_state()

        if config:
            pass  # TODO
        else:
            self.config = self.get_config(None)

    def write_reg(self, reg, data):
        return wp.wiringPiI2CWriteReg8(self.device_id, reg, data)

    def read_reg(self, reg):
        return wp.wiringPiI2CReadReg8(self.device_id, reg)

    def get_io_state(self):

        return self.get_state('input')

    def set_io_state(self, state):

        self.set_state('output', state)

    def set_state(self, reg, state):

        if reg not in self.regs:
            raise ValueError('Register {} does not exist. Available registers: {}'.format(reg, ', '.join(self.regs.keys())))

        if isinstance(state, bs.BitArray):
            pass
        elif isinstance(state, int):
            state = bs.BitArray('uint:{}={}'.format(self.n_gpio, state))
        elif isinstance(state, Iterable):
            state = bs.BitArray(state)
        else:
            raise ValueError('State must be integer, string or BitArray representing 2 Bytes')

        if len(state) != self.n_gpio:
            raise ValueError('State must be 2 Bytes')

        # Read values of the two ports input state
        for i, reg_ in enumerate(self.regs[reg]):
            port_state = state[i*8:(i+1)*8]
            if self.config[reg][i*8:(i+1)*8] != port_state:
                port_state.reverse()  # Match bit order with physical pin order, increasing left to right
                self.write_reg(reg=reg_, data=port_state.uint)

    def get_state(self, reg):

        if reg not in self.regs:
            raise ValueError('Register {} does not exist. Available registers: {}'.format(reg, ', '.join(self.regs.keys())))

        state = bs.BitArray(self.n_gpio)

        # Read values of the two ports input state
        for i, reg_ in enumerate(self.regs[reg]):
            port_state = bs.BitArray('uint:8={}'.format(self.read_reg(reg=reg_)))
            port_state.reverse()  # Match bit order with physical pin order, increasing left to right
            state[i*8:(i+1)*8] = port_state

        return state

    def set_output(self, pins=None):

        if pins is not None:
            # Get current io configuration state
            state = self.get_state(reg='config')
        else:
            # Set all pins as outputs
            self.set_state('config', [0]*self.n_gpio)

    def set_input(self, pins=None):
        if pins is not None:
            # Get current io configuration state
            state = self.get_state(reg='config')
        else:
            # Set all pins as outputs
            self.set_state('config', [1]*self.n_gpio)

    def set_bits_to_int(self, bits, val):

        state = self.get_io_state()

        val_bits = bs.BitArray('uint:{}={}'.format(len(bits), val))
        val_bits.reverse()

        for i, bit in enumerate(bits):
            state[bit] = val_bits[i]

        self.set_io_state(state)

    def get_int_from_bits(self, bits):

        state = self.get_io_state()

        val_bits = bs.BitArray([state[bit] for bit in bits])

        return val_bits.uint

    def get_config(self, representation='bin'):
        get_repr = lambda reg: getattr(self.get_state(reg), representation)
        return {reg: self.get_state(reg) if representation is None else get_repr(reg) for reg in self.regs}
