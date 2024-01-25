from collections.abc import Iterable
from functools import wraps
from threading import Event

from irrad_control.devices.arduino.serial_to_i2c.arduino_i2c import ArduinoToI2C


def _event_lock(io_func):
    """
    Decorator which allows the complete execution of *io_func* without a different
    thread calling another io functions which are decorated with this decorator.
    Uses threading.Event to make simply block other calls until done.
    """

    @wraps(io_func)
    def wrapper(*args, **kwargs):

        # Wait for flag to indicate that its available
        args[0]._device_available.wait()

        # Clear the available state and indicate we're now locking access
        args[0]._device_available.clear()

        res = io_func(*args, **kwargs)

        # Make access available again
        args[0]._device_available.set()

        return res

    return wrapper


class TCA9555(object):
    """
    This class implements a thread-safe interface to the 16-bit IO expander using the I2C-interface of an Arduino

    The TCA9555 consists of two 8-bit Configuration (input or output selection), Input Port, Output Port and
    Polarity Inversion (active high or active low operation) registers which are also referred to as ports:
    Port 0 covers the IO bits P[7:0], port 1 covers bits P[15:8] (P[17:10] in datasheet convention). The bit
    representation of the bit states hardware-wise is big-endian:

        128 == 0b10000000 == bit 7 high, all others low
          1 == 0b00000001 == bit 0 high, all others low

    The default of representing the bit states within this class is to order by actual bit indices

        '10000000' ==  bit 0 high, all others low
        '00000001' == bit 7 high, all others low

    All public methods are thread-safe
    """

    # Internal registers of (port_0, port_1)
    regs = {
        # Registers holding the actual values of the pin levels
        "input": (0x00, 0x01),
        # Registers holding the target values of pin levels
        "output": (0x02, 0x03),
        # Registers holding the polarity (active-high or active-low)
        "polarity": (0x04, 0x05),
        # Registers holding whether the pins are configured as in- (1) or output (0)
        "config": (0x06, 0x07),
    }

    # Number of available io bits; bits are shared into ports
    _n_io_bits = 16

    # Number of bits of one port
    _n_bits_per_port = 8

    # Number of ports of TCA9555
    _n_ports = 2

    def __init__(self, port, address=0x20, config=None):
        """
        Initialize the connection to the chip and set the a configuration if given

        port: str
            file descriptor of serial port under which the Arduino sits
        address: int
            integer of the I2C address of the TCA9555 (default is 0x20 e.g. 32)
        config: dict
            dictionary holding register values which should be set
        """

        # I2C-bus address; 0x20 (32 in decimal) if all address pins A0=A1=A2 are low
        self.address = address

        # Use arduino to communicate via I2C
        self._intf = ArduinoToI2C(port=port, address=address)

        # Flag which indicates writing or reading condition
        self._device_available = Event()
        self._device_available.set()

        if config:
            self.config = config

    @property
    def device_busy(self):
        return not self._device_available.is_set()

    @device_busy.setter
    def device_busy(self, val):
        raise ValueError("This is a read-only property")

    @property
    @_event_lock
    def io_state(self):
        return self._get_state("input")

    @io_state.setter
    @_event_lock
    def io_state(self, state):
        self._set_state("output", state)

    @property
    def n_io_bits(self):
        return self._n_io_bits

    @n_io_bits.setter
    def n_io_bits(self, val):
        raise ValueError("This is a read-only property")

    @property
    def n_bits_per_port(self):
        return self._n_bits_per_port

    @n_bits_per_port.setter
    def n_bits_per_port(self, val):
        raise ValueError("This is a read-only property")

    @property
    def n_ports(self):
        return self._n_ports

    @n_ports.setter
    def n_ports(self, val):
        raise ValueError("This is a read-only property")

    @property
    @_event_lock
    def config(self):
        return {reg: self._get_state(reg) for reg in self.regs}

    @config.setter
    @_event_lock
    def config(self, config):
        for reg, val in config.items():
            self._set_state(reg, val)

    def _check_con(self):
        """
        Check the connection from arduino to port
        """
        return self._intf.check_i2c_con(self)

    def _write_reg(self, reg, data):
        """
        Writes one byte of *data* to register *reg*
        reg: int
            register value to write byte to
        data: 8 bit
            8 bit of data to write

        Returns
        -------
        Integer indicating successful write
        """
        return self._intf.write_register(reg=reg, data=data)

    def _read_reg(self, reg):
        """
        Reads one byte of *data* from register *reg*

        Parameters
        ----------
        reg: int
            register value to write byte to

        Returns
        -------
        8 bit of data read from *reg*
        """
        return self._intf.read_register(reg=reg)

    def _create_state(self, state_repr, bit_length):
        """
        Method to create a string-representation of the desired *state_repr* of *bit_length* bits e.g.

        self._create_state(3, bit_length=8)  # '00000011'

        Parameters
        ----------
        state_repr: int, str, Iterable
            state from which to create a string-representation of
        bit_length: int
            length of the state
        Returns
        -------
        state: str
            The representation of the state as a binary string
        """
        
        if isinstance(state_repr, int):
            state = format(state_repr, f'0{bit_length}b')

        elif isinstance(state_repr, Iterable):
            state = ''
            for i in state_repr:
                state += f'{int(i):1b}'

            if len(state) < bit_length:
                state = '0' * (bit_length - len(state)) + state
        else:
            raise ValueError(
                "State must be integer, iterable or string representing {} bits".format(bit_length)
            )

        if len(state) != bit_length:
            raise ValueError("State must be {} bits, is {}".format(bit_length, len(state)))

        return state

    def _check_register(self, reg):
        """
        Checks if the register *reg* exists

        Parameters
        ----------
        reg: str
            String of register name whose existence is checked
        """
        if reg not in self.regs:
            raise ValueError(
                "Register {} does not exist. Available registers: {}".format(
                    reg, ", ".join(self.regs.keys())
                )
            )

    def _check_bits(self, bits, val=None):
        """
        Checks if the an operation on the IO bits is valid

        Parameters
        ----------
        bits: int, Iterable of ints
            Iterable of bits on which an operation is performed
        val: int, None
            If not None, *val* must be an integer with bit length <= number of bits e.g. len(*bits*)
        """
        bits = bits if isinstance(bits, Iterable) else [bits]

        if any(not 0 <= b < self._n_io_bits for b in bits):
            raise IndexError(
                "{}'s {} bits are indexed from {} to {}".format(
                    self.__class__.__name__, self._n_io_bits, 0, self._n_io_bits - 1
                )
            )

        if len(set(bits)) != len(bits):
            raise IndexError("Duplicate bit indices! *bits* must be composed of unique bit indices")

        if val:
            if val.bit_length() > len(bits):
                raise ValueError(
                    "Too little bits. Bit length of value {} is {}, the number of bits is {}".format(
                        val, val.bit_length(), len(bits)
                    )
                )

        return bits

    def _set_bits(self, reg, val=1, bits=None):
        """
        Get the current state of an individual port of the TCA9555

        Parameters
        ----------
        reg: str
            Name of register whose state will be read
        val: int, bool
            Either 0 or 1
        bits: Iterable, int
            bits to set to *val*
        """
        if val not in (0, 1, True, False):
            raise ValueError("'val' can only be 1 or 0")

        if bits is not None:
            # Check if bit indices and values are fine
            bits = self._check_bits(bits=bits)

            # Get current io configuration state
            state = self._get_state(reg=reg)

            # Loop over state and set bits
            new_state = ''
            for i in range(len(state)):
                if i in bits:
                    new_state += f'{val:1b}'
                else:
                    new_state += state[i]

            # Set state
            self._set_state(reg, new_state)

        else:
            # Set all pins to *val*
            self._set_state(reg, [val] * self._n_io_bits)

    def _set_state(self, reg, state):
        """
        Set the *state* to the register *reg*

        Parameters
        ----------
        reg: str
            Name of register whose state will be set
        state: str, int, Iterable, 
            Value from which a string-representation of *state* can be created
        """
        # Create empty target register state
        target_reg_state = self._create_state(state, self._n_io_bits)

        # loop over individual ports
        for port in range(self._n_ports):

            # Compare individual current port states with target port states
            target_port_state = target_reg_state[
                port * self._n_bits_per_port : (port + 1) * self._n_bits_per_port
            ]

            # If target and current state differ, write
            if target_port_state != self._get_port_state(reg=reg, port=port):
                self._set_port_state(reg=reg, port=port, state=target_port_state)

    def _set_port_state(self, reg, port, state):
        """
        Get the current state of an individual port of the TCA9555

        Parameters
        ----------
        reg: str
            Name of register whose state will be set
        port: int
            Index of the port; either 0 or 1
        state: str, int, Iterable
            Value from which a string-representation of *state* can be created
        """
        # Check if register exists
        self._check_register(reg)

        if port not in (0, 1):
            raise IndexError("*port* must be index of physical port; either 0 or 1")

        target_state = self._create_state(state, bit_length=self._n_bits_per_port)

        # Match bit order with physical pin order, increasing left to right
        reg_data = int(target_state[::-1], base=2)

        self._write_reg(reg=self.regs[reg][port], data=reg_data)

    def _get_state(self, reg):
        """
        Get the *state* to the register *reg*

        Parameters
        ----------
        reg: str
            Name of register whose state will be read
        """
        return ''.join(self._get_port_state(reg=reg, port=port) for port in range(self._n_ports))

    def _get_port_state(self, reg, port):
        """
        Get the current state of an individual port of the TCA9555

        Parameters
        ----------
        reg: str
            Name of register whose state will be read
        port: int
            Index of the port; either 0 or 1
        """
        # Check if register exists
        self._check_register(reg)

        if port not in (0, 1):
            raise IndexError("*port* must be index of physical port; either 0 or 1")

        # Read port state
        port_state = self._create_state(self._read_reg(reg=self.regs[reg][port]),
                                        bit_length=self._n_bits_per_port)

        # Match bit order with physical pin order, increasing left to right
        return port_state[::-1]

    @_event_lock
    def int_to_bits(self, bits, val):
        """
        Method to set *bits* to state that represents *val*

        Parameters
        ----------
        bits: Iterable, int
            bits which represent value *val*
        val: int
            Integer which should be represented though *bits* binary state
        """
        # Get the actual logic levels which are applied to the pins
        state = self._get_state("input")

        # Create state for set of bits
        val_bits = self._create_state(val, bit_length=len(bits))

        # Match bit order with physical pin order, increasing left to right
        val_bits = val_bits[::-1]

        # Update current io state
        new_state = ''
        for i in range(len(state)):
            if i in bits:
                new_state += val_bits[bits.index(i)]
            else:
                new_state += state[i]

        # Set the updated state
        self._set_state("output", new_state)

    def int_from_bits(self, bits):
        """
        Method to get binary value from a set of *bits*

        Parameters
        ----------
        bits: Iterable, int
            bits from which to read the integer
        """
        # Get the actual logic levels which are applied to the pins
        state = self.io_state

        # Read the respective bit values
        bit_state = ''
        for bit in bits:
            bit_state += state[bit]

        # Match bit order with physical pin order, increasing left to right
        val = int(bit_state[::-1], base=2)

        return val

    @_event_lock
    def set_state(self, reg, state):
        """
        Thread-safe version of the private *_set_state*-method

        Parameters
        ----------
        reg: str
            Name of register whose state will be set
        state: str, int, Iterable
            Value from which a string-representation of *state* can be created
        """
        self._set_state(reg=reg, state=state)

    @_event_lock
    def set_port_state(self, reg, port, state):
        """
        Thread-safe version of the private *_set_port_state*-method

        Parameters
        ----------
        reg: str
            Name of register whose state will be set
        port: int
            Index of the port; either 0 or 1
        state: str, int, Iterable
            Value from which a string-representation of *state* can be created
        """
        self._set_port_state(reg=reg, port=port, state=state)

    @_event_lock
    def get_state(self, reg):
        """
        Thread-safe version of the private *_get_state*-method

        Parameters
        ----------
        reg: str
            Name of register whose state will be read
        """
        self._get_state(reg=reg)

    @_event_lock
    def get_port_state(self, reg, port):
        """
        Thread-safe version of the private *_get_port_state*-method

        Parameters
        ----------
        reg: str
            Name of register whose state will be read
        port: int
            Index of the port; either 0 or 1
        """
        self._get_port_state(reg=reg, port=port)

    def is_high(self, bit):
        """
        Method to get logical state of single bit

        Parameters
        ----------
        bit: int
            bit from which to read the state
        """
        self._check_bits(bits=bit)

        return bool(int(self.io_state[bit]))

    @_event_lock
    def set_direction(self, direction, bits=None):
        """
        Convenience-method to set direction of bits: input (1) or output (0)

        Parameters
        ----------
        direction: int
            1 for input, 0 for output
        bits: Iterable, int, None
            bits for which the direction will be set
        """
        self._set_bits(reg="config", val=int(bool(direction)), bits=bits)

    @_event_lock
    def set_polarity(self, polarity, bits=None):
        """
        Convenience-method to set polarity of bits: active-high (0) or active-low (1)

        Parameters
        ----------
        polarity: int
            1 for inversion, 0 for default
        bits: Iterable, int, None
            bits for which the polarity will be set
        """
        self._set_bits(reg="polarity", val=int(bool(polarity)), bits=bits)

    @_event_lock
    def set_level(self, level, bits=None):
        """
        Convenience-method to set logic-level of bits

        Parameters
        ----------
        level: int
            1 for logical high, 0 for logic 0
        bits: Iterable, int, None
            bits for which the level will be set
        """
        self._set_bits(reg="output", val=int(bool(level)), bits=bits)

    def format_config(self, format_="#16b"):
        """
        Method returning a more readable version of self.config

        Parameters
        ----------
        format_: str
            Any attribute of BitArray-class
        """
        fmt = lambda s: format(int(s, base=2), format_) if format_ is not None else s 
        return {reg: fmt(state) for reg, state in self.config.items()}

    def set_bits(self, bits=None):
        """
        Convenience-method to set bits e.g. set the output level to logical 1

        Parameters
        ----------
        bits: Iterable, int, None
            bits of *reg* which will be set (to 1)
        """
        self.set_level(level=1, bits=bits)

    def unset_bits(self, bits=None):
        """
        Convenience-method to unset *bits* e.g. set the output level to logical 0

        Parameters
        ----------
        bits: Iterable, int, None
            bits of *reg* which will be unset (to 0)
        """
        self.set_level(level=0, bits=bits)
