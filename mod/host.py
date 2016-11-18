# -*- coding: utf-8 -*-

# Copyright 2012-2013 AGR Audio, Industria e Comercio LTDA. <contato@portalmod.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
This module works as an interface for mod-host, it uses a socket to communicate
with mod-host, the protocol is described in <http://github.com/portalmod/mod-host>

The module relies on tornado.ioloop stuff, but you need to start the ioloop
by yourself:

>>> from tornado import ioloop
>>> ioloop.IOLoop.instance().start()

This will start the mainloop and will handle the callbacks and the async functions
"""

from collections import OrderedDict
from random import randint
from shutil import rmtree
from tornado import gen, iostream, ioloop
import os, json, socket, logging

from mod import get_hardware, symbolify
from mod.bank import list_banks, get_last_bank_and_pedalboard, save_last_bank_and_pedalboard
from mod.protocol import Protocol, ProtocolError, process_resp
from mod.utils import (charPtrToString,
                       is_bundle_loaded, add_bundle_to_lilv_world, remove_bundle_from_lilv_world, rescan_plugin_presets,
                       get_plugin_info, get_plugin_control_inputs_and_monitored_outputs,
                       get_pedalboard_info, get_state_port_values, list_plugins_in_bundle,
                       get_all_pedalboards, get_pedalboard_plugin_values,
                       init_jack, close_jack, get_jack_data, init_bypass,
                       get_jack_port_alias, get_jack_hardware_ports, has_serial_midi_input_port, has_serial_midi_output_port,
                       connect_jack_ports, disconnect_jack_ports, get_truebypass_value, set_util_callbacks)
from mod.settings import (DEFAULT_PEDALBOARD, LV2_PEDALBOARDS_DIR,
                          TUNER_URI, TUNER_INSTANCE, TUNER_INPUT_PORT, TUNER_MONITOR_PORT)
from mod.tuner import find_freqnotecents

ADDRESSING_CTYPE_LINEAR       = 0
ADDRESSING_CTYPE_BYPASS       = 1
ADDRESSING_CTYPE_TAP_TEMPO    = 2
ADDRESSING_CTYPE_ENUMERATION  = 4 # implies scalepoints
ADDRESSING_CTYPE_SCALE_POINTS = 8
ADDRESSING_CTYPE_TRIGGER      = 16
ADDRESSING_CTYPE_TOGGLED      = 32
ADDRESSING_CTYPE_LOGARITHMIC  = 64
ADDRESSING_CTYPE_INTEGER      = 128

ACTUATOR_TYPE_FOOTSWITCH = 1
ACTUATOR_TYPE_KNOB       = 2
ACTUATOR_TYPE_POT        = 3

BANK_CONFIG_NOTHING         = 0
BANK_CONFIG_TRUE_BYPASS     = 1
BANK_CONFIG_PEDALBOARD_UP   = 2
BANK_CONFIG_PEDALBOARD_DOWN = 3

HARDWARE_TYPE_MOD    = 0
HARDWARE_TYPE_PEDAL  = 1
HARDWARE_TYPE_TOUCH  = 2
HARDWARE_TYPE_ACCEL  = 3
HARDWARE_TYPE_CUSTOM = 4

# Special URI for non-addressed controls
kNullAddressURI = "null"

# Special URIs for midi-learn
kMidiLearnURI = "/midi-learn"
kMidiUnmapURI = "/midi-unmap"
kMidiCustomPrefixURI = "/midi-custom_" # to show current one

# Limits
kMaxAddressableScalepoints = 50

def get_all_good_pedalboards():
    allpedals  = get_all_pedalboards()
    goodpedals = []

    for pb in allpedals:
        if not pb['broken']:
            goodpedals.append(pb)

    return goodpedals

# class to map between numeric ids and string instances
class InstanceIdMapper(object):
    def __init__(self):
        self.clear()

    def clear(self):
        # last used id, always incrementing
        self.last_id = 0
        # map id <-> instances
        self.id_map = {}
        # map instances <-> ids
        self.instance_map = {}

    # get a numeric id from a string instance
    def get_id(self, instance):
        # check if it already exists
        if instance in self.instance_map.keys():
            return self.instance_map[instance]

        # increment last id
        idx = self.last_id
        self.last_id += 1

        # create mapping
        self.instance_map[instance] = idx
        self.id_map[idx] = instance

        # ready
        return self.instance_map[instance]

    def get_id_without_creating(self, instance):
        return self.instance_map[instance]

    # get a string instance from a numeric id
    def get_instance(self, id):
        return self.id_map[id]

class Host(object):
    def __init__(self, hmi, msg_callback):
        if False:
            from mod.hmi import HMI
            self.hmi = HMI()

        self.hmi = hmi
        self.addr = ("localhost", 5555)
        self.readsock = None
        self.writesock = None
        self.crashed = False
        self.connected = False
        self.current_tuner_port = 1
        self._queue = []
        self._idle = True
        self.mapper = InstanceIdMapper()
        self.banks = list_banks()
        self.allpedalboards = None
        self.bank_id = 0
        self.plugins = {}
        self.connections = []
        self.audioportsIn = []
        self.audioportsOut = []
        self.midiports = [] # [symbol, alias, pending-connections]
        self.hasSerialMidiIn = False
        self.hasSerialMidiOut = False
        self.pedalboard_empty    = True
        self.pedalboard_modified = False
        self.pedalboard_name     = ""
        self.pedalboard_path     = ""
        self.pedalboard_size     = [0,0]
        self.pedalboard_presets  = []
        self.next_hmi_pedalboard = None

        self.statstimer = ioloop.PeriodicCallback(self.statstimer_callback, 1000)

        if os.path.exists("/proc/meminfo"):
            self.memfile  = open("/proc/meminfo", 'r')
            self.memtotal = 0.0
            self.memfseek = 0
            self.memfskip = False
            wasFreeBefore = False

            for line in self.memfile.readlines():
                if line.startswith("MemTotal:"):
                    self.memtotal = float(int(line.replace("MemTotal:","",1).replace("kB","",1).strip()))
                elif line.startswith("MemFree:"):
                    wasFreeBefore = True
                    continue
                elif wasFreeBefore:
                    if line.startswith("MemAvailable:"):
                        self.memfskip = True
                        break
                    elif line.startswith("Buffers:"):
                        break
                wasFreeBefore = False
                self.memfseek += len(line)
            else:
                self.memfseek = 0

            if self.memtotal != 0.0 and self.memfseek != 0:
                self.memtimer = ioloop.PeriodicCallback(self.memtimer_callback, 5000)

        else:
            self.memtimer = None

        self.msg_callback = msg_callback

        set_util_callbacks(self.midi_port_appeared, self.midi_port_deleted, self.true_bypass_changed)

        # Register HMI protocol callbacks
        self._init_addressings()
        Protocol.register_cmd_callback("hw_con", self.hmi_hardware_connected)
        Protocol.register_cmd_callback("hw_dis", self.hmi_hardware_disconnected)
        Protocol.register_cmd_callback("banks", self.hmi_list_banks)
        Protocol.register_cmd_callback("pedalboards", self.hmi_list_bank_pedalboards)
        Protocol.register_cmd_callback("pedalboard", self.hmi_load_bank_pedalboard)
        Protocol.register_cmd_callback("control_get", self.hmi_parameter_get)
        Protocol.register_cmd_callback("control_set", self.hmi_parameter_set)
        Protocol.register_cmd_callback("control_next", self.hmi_parameter_addressing_next)
        Protocol.register_cmd_callback("control_prev", self.hmi_parameter_addressing_prev)
        Protocol.register_cmd_callback("pedalboard_save", self.hmi_save_current_pedalboard)
        Protocol.register_cmd_callback("pedalboard_reset", self.hmi_reset_current_pedalboard)
        Protocol.register_cmd_callback("tuner", self.hmi_tuner)
        Protocol.register_cmd_callback("tuner_input", self.hmi_tuner_input)

        ioloop.IOLoop.instance().add_callback(self.init_host)

    def __del__(self):
        self.msg_callback("stop")
        self.close_jack()

    def midi_port_appeared(self, name, isOutput):
        name = charPtrToString(name)
        isOutput = bool(isOutput)

        alias = get_jack_port_alias(name)
        if not alias:
            return
        alias = alias.split("-",5)[-1].replace("-"," ").replace(";",".")

        if not isOutput:
            connect_jack_ports(name, "mod-host:midi_in")

        for i in range(len(self.midiports)):
            port_symbol, port_alias, port_conns = self.midiports[i]
            if alias == port_alias or (";" in port_alias and alias in port_alias.split(";",1)):
                split = port_symbol.split(";")

                if len(split) == 1:
                    oldnode = "/graph/" + port_symbol.split(":",1)[-1]
                    port_symbol = name
                else:
                    if isOutput:
                        oldnode = "/graph/" + split[1].split(":",1)[-1]
                        split[1] = name
                    else:
                        oldnode = "/graph/" + split[0].split(":",1)[-1]
                        split[0] = name
                    port_symbol = ";".join(split)

                self.midiports[i][0] = port_symbol
                break
        else:
            return

        index = int(name[-1])
        title = self.get_port_name_alias(name).replace("-","_").replace(" ","_")
        newnode = "/graph/" + name.split(":",1)[-1]

        if name.startswith("nooice"):
            index += 100

        self.msg_callback("add_hw_port /graph/%s midi %i %s %i" % (name.split(":",1)[-1], int(isOutput), title, index))

        for i in reversed(range(len(port_conns))):
            if port_conns[i][0] == oldnode:
                port_conns[i] = (newnode, port_conns[i][1])
            elif port_conns[i][1] == oldnode:
                port_conns[i] = (port_conns[i][0], newnode)
            connection = port_conns[i]

            if newnode not in connection:
                continue
            if not connect_jack_ports(self._fix_host_connection_port(connection[0]),
                                      self._fix_host_connection_port(connection[1])):
                continue

            self.connections.append(connection)
            self.msg_callback("connect %s %s" % (connection[0], connection[1]))
            port_conns.pop(i)

    def midi_port_deleted(self, name):
        name = charPtrToString(name)
        removed_conns = []

        for ports in self.connections:
            jackports = (self._fix_host_connection_port(ports[0]), self._fix_host_connection_port(ports[1]))
            if name not in jackports:
                continue
            disconnect_jack_ports(jackports[0], jackports[1])
            removed_conns.append(ports)

        for ports in removed_conns:
            self.connections.remove(ports)
            disconnect_jack_ports(ports[0], ports[1])

        for port_symbol, port_alias, port_conns in self.midiports:
            if name == port_symbol or (";" in port_symbol and name in port_symbol.split(";",1)):
                port_conns += removed_conns
                break

        self.msg_callback("remove_hw_port /graph/%s" % (name.split(":",1)[-1]))

    def true_bypass_changed(self, left, right):
        self.msg_callback("truebypass %i %i" % (left, right))

    # -----------------------------------------------------------------------------------------------------------------
    # Initialization

    @gen.coroutine
    def init_host(self):
        self.init_jack()
        self.open_connection_if_needed(None)

        if self.allpedalboards is None:
            self.allpedalboards = get_all_good_pedalboards()

        bank_id, pedalboard = get_last_bank_and_pedalboard()

        yield gen.Task(self.send_notmodified, "remove -1", datatype='boolean')

        # FIXME: ensure HMI is initialized by now

        if pedalboard:
            self.bank_id = bank_id
            self.load(pedalboard)

        else:
            self.bank_id = 0

            if os.path.exists(DEFAULT_PEDALBOARD):
                self.load(DEFAULT_PEDALBOARD, True)

        if self.bank_id > 0 and pedalboard and self.bank_id <= len(self.banks):
            bank = self.banks[self.bank_id-1]
            navigateFootswitches = bank['navigateFootswitches']
            navigateChannel      = int(bank['navigateChannel'])-1 if "navigateChannel" in bank.keys() and not navigateFootswitches else 15
        else:
            navigateFootswitches = False
            navigateChannel      = 15

        self.send_notmodified("midi_program_listen %d %d" % (int(not navigateFootswitches), navigateChannel))
        self.send_notmodified("output_data_ready")
        init_bypass()

    def init_jack(self):
        self.audioportsIn  = []
        self.audioportsOut = []

        if not init_jack():
            return

        for port in get_jack_hardware_ports(True, False):
            self.audioportsIn.append(port.split(":",1)[-1])

        for port in get_jack_hardware_ports(True, True):
            self.audioportsOut.append(port.split(":",1)[-1])

    def close_jack(self):
        close_jack()

    def open_connection_if_needed(self, websocket):
        if self.readsock is not None and self.writesock is not None:
            self.report_current_state(websocket)
            return

        def reader_check_response():
            self.process_read_queue()

        def writer_check_response():
            self.connected = True
            self.report_current_state(websocket)
            self.statstimer.start()

            if self.memtimer is not None:
                self.memtimer_callback()
                self.memtimer.start()

            if len(self._queue):
                self.process_write_queue()
            else:
                self._idle = True

        self._idle = False

        # Main socket, used for sending messages
        self.writesock = iostream.IOStream(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        self.writesock.set_close_callback(self.writer_connection_closed)
        self.writesock.set_nodelay(True)
        self.writesock.connect(self.addr, writer_check_response)

        # Extra socket, used for receiving messages
        self.readsock = iostream.IOStream(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        self.readsock.set_close_callback(self.reader_connection_closed)
        self.readsock.set_nodelay(True)
        self.readsock.connect((self.addr[0], self.addr[1]+1), reader_check_response)

    def reader_connection_closed(self):
        self.readsock = None

    def writer_connection_closed(self):
        self.writesock = None
        self.crashed = True
        self.statstimer.stop()

        if self.memtimer is not None:
            self.memtimer.stop()

        self.msg_callback("stop")

    # -----------------------------------------------------------------------------------------------------------------

    def setNavigateWithFootswitches(self, enabled, callback):
        def foot2_callback(ok):
            acthw  = self._uri2hw_map["/hmi/footswitch2"]
            cfgact = BANK_CONFIG_PEDALBOARD_UP if enabled else BANK_CONFIG_NOTHING
            self.hmi.bank_config(acthw[0], acthw[1], acthw[2], acthw[3], cfgact, callback)

        acthw  = self._uri2hw_map["/hmi/footswitch1"]
        cfgact = BANK_CONFIG_PEDALBOARD_DOWN if enabled else BANK_CONFIG_NOTHING
        self.hmi.bank_config(acthw[0], acthw[1], acthw[2], acthw[3], cfgact, foot2_callback)

    def initialize_hmi(self, uiConnected, callback):
        # If UI is already connected, do nothing
        if uiConnected:
            callback(True)
            return

        bank_id, pedalboard = get_last_bank_and_pedalboard()

        # report pedalboard and banks
        if bank_id > 0 and pedalboard and bank_id <= len(self.banks):
            bank = self.banks[bank_id-1]
            pedalboards = bank['pedalboards']
            navigateFootswitches = bank['navigateFootswitches']
            if "navigateChannel" in bank.keys() and not navigateFootswitches:
                navigateChannel = int(bank['navigateChannel'])-1
            else:
                navigateChannel = 15

        else:
            if self.allpedalboards is None:
                self.allpedalboards = get_all_good_pedalboards()
            bank_id = 0
            pedalboard = DEFAULT_PEDALBOARD
            pedalboards = self.allpedalboards
            navigateFootswitches = False
            navigateChannel = 15

        num = 0
        for pb in pedalboards:
            if pb['bundle'] == pedalboard:
                pedalboard_id = num
                break
            num += 1

        else:
            bank_id = 0
            pedalboard_id = 0
            pedalboard = ""
            pedalboards = []

        def footswitch_callback(ok):
            self.setNavigateWithFootswitches(True, callback)

        def midi_prog_callback(ok):
            self.send_notmodified("midi_program_listen 1 %d" % navigateChannel, callback, datatype='boolean')

        def initial_state_callback(ok):
            cb = footswitch_callback if navigateFootswitches else midi_prog_callback
            self.hmi.initial_state(bank_id, pedalboard_id, pedalboards, cb)

        self.setNavigateWithFootswitches(False, initial_state_callback)

    def start_session(self, callback):
        if not self.hmi.initialized:
            callback(True)
            return

        def footswitch_addr2_callback(ok):
            acthw = self._uri2hw_map["/hmi/footswitch2"]
            self._address_next(acthw, callback)

        def footswitch_addr1_callback(ok):
            acthw = self._uri2hw_map["/hmi/footswitch1"]
            self._address_next(acthw, footswitch_addr2_callback)

        def footswitch_bank_callback(ok):
            self.setNavigateWithFootswitches(False, footswitch_addr1_callback)

        self.send_notmodified("midi_program_listen 0 -1")

        self.banks = []
        self.allpedalboards = []
        self.hmi.ui_con(footswitch_bank_callback)

    def end_session(self, callback):
        if not self.hmi.initialized:
            callback(True)
            return

        def initialize_callback(ok):
            self.initialize_hmi(False, callback)

        self.banks = list_banks()
        self.allpedalboards = get_all_good_pedalboards()
        self.hmi.ui_dis(initialize_callback)

    # -----------------------------------------------------------------------------------------------------------------
    # Message handling

    def process_read_queue(self):
        def check_message(msg):
            msg = msg[:-1].decode("utf-8", errors="ignore")
            logging.info("[host] received <- %s" % repr(msg))

            msg = msg.split()
            cmd = msg[0]

            if cmd == "param_set":
                instance_id = int(msg[1])
                portsymbol  = msg[2]
                value       = float(msg[3])

                try:
                    instance = self.mapper.get_instance(instance_id)
                    plugin   = self.plugins[instance_id]
                except:
                    pass
                else:
                    if portsymbol == ":bypass":
                        plugin['bypassed'] = bool(value)
                    else:
                        plugin['ports'][portsymbol] = value
                    self.pedalboard_modified = True
                    self.msg_callback("param_set %s %s %f" % (instance, portsymbol, value))

            elif cmd == "output_set":
                instance_id = int(msg[1])
                portsymbol  = msg[2]
                value       = float(msg[3])

                if instance_id == TUNER_INSTANCE:
                    self.set_tuner_value(value)

                else:
                    try:
                        instance = self.mapper.get_instance(instance_id)
                        plugin   = self.plugins[instance_id]
                    except:
                        pass
                    else:
                        plugin['outputs'][portsymbol] = value
                        self.msg_callback("output_set %s %s %f" % (instance, portsymbol, value))

            elif cmd == "midi_mapped":
                instance_id = int(msg[1])
                portsymbol  = msg[2]
                channel     = int(msg[3])
                controller  = int(msg[4])
                value       = float(msg[5])
                minimum     = float(msg[6])
                maximum     = float(msg[7])

                instance = self.mapper.get_instance(instance_id)

                if portsymbol == ":bypass":
                    self.plugins[instance_id]['bypassCC'] = (channel, controller)
                    self.plugins[instance_id]['bypassed'] = bool(value)
                else:
                    self.plugins[instance_id]['midiCCs'][portsymbol] = (channel, controller, minimum, maximum)
                    self.plugins[instance_id]['ports'][portsymbol] = value

                self.pedalboard_modified = True
                self.msg_callback("midi_map %s %s %i %i %f %f" % (instance, portsymbol,
                                                                  channel, controller,
                                                                  minimum, maximum))
                self.msg_callback("param_set %s %s %f" % (instance, portsymbol, value))

            elif cmd == "midi_program":
                program = int(msg[1])
                bank_id = self.bank_id

                if self.bank_id > 0 and self.bank_id <= len(self.banks):
                    pedalboards = self.banks[self.bank_id-1]['pedalboards']
                else:
                    pedalboards = self.allpedalboards

                if program >= 0 and program < len(pedalboards):
                    bundlepath = pedalboards[program]['bundle']

                    def load_callback(ok):
                        self.bank_id = bank_id
                        self.load(bundlepath)

                    def hmi_clear_callback(ok):
                        self.hmi.clear(load_callback)

                    self.reset(hmi_clear_callback)

            elif cmd == "data_finish":
                self.send_output_data_ready()

            else:
                logging.error("[host] unrecognized command: %s" % cmd)

            self.process_read_queue()

        if self.readsock is None:
            return

        self.readsock.read_until(b"\0", check_message)

    @gen.coroutine
    def send_output_data_ready(self):
        yield gen.Task(self.send_notmodified, "output_data_ready", datatype='boolean')

    def process_write_queue(self):
        try:
            msg, callback, datatype = self._queue.pop(0)
            logging.info("[host] popped from queue: %s" % msg)
        except IndexError:
            self._idle = True
            return

        if self.writesock is None:
            self.process_write_queue()
            return

        def check_response(resp):
            if callback is not None:
                resp = resp.decode("utf-8", errors="ignore")
                logging.info("[host] received <- %s" % repr(resp))

                if datatype == 'string':
                    r = resp
                elif not resp.startswith("resp"):
                    logging.error("[host] protocol error: %s" % ProtocolError(resp))
                    r = None
                else:
                    r = resp.replace("resp ", "").replace("\0", "").strip()

                callback(process_resp(r, datatype))

            self.process_write_queue()

        self._idle = False
        logging.info("[host] sending -> %s" % msg)

        encmsg = "%s\0" % str(msg)
        self.writesock.write(encmsg.encode("utf-8"))
        self.writesock.read_until(b"\0", check_response)

    # send data to host, set modified flag to true
    def send_modified(self, msg, callback=None, datatype='int'):
        self.pedalboard_modified = True
        self._queue.append((msg, callback, datatype))
        if self._idle:
            self.process_write_queue()

    # send data to host, don't change modified flag
    def send_notmodified(self, msg, callback=None, datatype='int'):
        self._queue.append((msg, callback, datatype))
        if self._idle:
            self.process_write_queue()

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff

    def mute(self):
        disconnect_jack_ports("mod-host:monitor-out_1", "system:playback_1")
        disconnect_jack_ports("mod-host:monitor-out_2", "system:playback_2")
        disconnect_jack_ports("mod-host:monitor-out_1", "mod-peakmeter:in_3")
        disconnect_jack_ports("mod-host:monitor-out_2", "mod-peakmeter:in_4")

    def unmute(self):
        connect_jack_ports("mod-host:monitor-out_1", "system:playback_1")
        connect_jack_ports("mod-host:monitor-out_2", "system:playback_2")
        connect_jack_ports("mod-host:monitor-out_1", "mod-peakmeter:in_3")
        connect_jack_ports("mod-host:monitor-out_2", "mod-peakmeter:in_4")

    def report_current_state(self, websocket):
        if websocket is None:
            return

        data = get_jack_data()
        websocket.write_message("mem_load " + self.get_free_memory_value())
        websocket.write_message("stats %0.1f %i" % (data['cpuLoad'], data['xruns']))
        websocket.write_message("truebypass %i %i" % (get_truebypass_value(False), get_truebypass_value(True)))
        websocket.write_message("loading_start %d %d" % (self.pedalboard_empty, self.pedalboard_modified))
        websocket.write_message("size %d %d" % (self.pedalboard_size[0], self.pedalboard_size[1]))

        crashed = self.crashed
        self.crashed = False

        if crashed:
            self.init_jack()

        midiports = []
        for port_id, port_alias, _ in self.midiports:
            if ";" in port_id:
                inp, outp = port_id.split(";",1)
                midiports.append(inp)
                midiports.append(outp)
            else:
                midiports.append(port_id)

        self.hasSerialMidiIn  = has_serial_midi_input_port()
        self.hasSerialMidiOut = has_serial_midi_output_port()

        # Audio In
        for i in range(len(self.audioportsIn)):
            name  = self.audioportsIn[i]
            title = name.title().replace(" ","_")
            websocket.write_message("add_hw_port /graph/%s audio 0 %s %i" % (name, title, i+1))

        # Audio Out
        for i in range(len(self.audioportsOut)):
            name  = self.audioportsOut[i]
            title = name.title().replace(" ","_")
            websocket.write_message("add_hw_port /graph/%s audio 1 %s %i" % (name, title, i+1))

        # MIDI In
        if self.hasSerialMidiIn:
            websocket.write_message("add_hw_port /graph/serial_midi_in midi 0 Serial_MIDI_In 0")

        ports = get_jack_hardware_ports(False, False)
        for i in range(len(ports)):
            name = ports[i]
            if name not in midiports:
                continue
            alias = get_jack_port_alias(name)
            if alias:
                title = alias.split("-",5)[-1].replace("-","_").replace(";",".")
            else:
                title = name.split(":",1)[-1].title().replace(" ","_")
            websocket.write_message("add_hw_port /graph/%s midi 0 %s %i" % (name.split(":",1)[-1], title, i+1))

        # MIDI Out
        if self.hasSerialMidiOut:
            websocket.write_message("add_hw_port /graph/serial_midi_out midi 1 Serial_MIDI_Out 0")

        ports = get_jack_hardware_ports(False, True)
        for i in range(len(ports)):
            name = ports[i]
            if name not in midiports:
                continue
            alias = get_jack_port_alias(name)
            if alias:
                title = alias.split("-",5)[-1].replace("-","_").replace(";",".")
            else:
                title = name.split(":",1)[-1].title().replace(" ","_")
            websocket.write_message("add_hw_port /graph/%s midi 1 %s %i" % (name.split(":",1)[-1], title, i+1))

        for instance_id, plugin in self.plugins.items():
            websocket.write_message("add %s %s %.1f %.1f %d" % (plugin['instance'], plugin['uri'],
                                                                plugin['x'], plugin['y'], int(plugin['bypassed'])))

            if -1 not in plugin['bypassCC']:
                mchnnl, mctrl = plugin['bypassCC']
                websocket.write_message("midi_map %s :bypass %i %i 0.0 1.0" % (plugin['instance'], mchnnl, mctrl))

            if plugin['preset']:
                websocket.write_message("preset %s %s" % (plugin['instance'], plugin['preset']))

            if crashed:
                self.send_notmodified("add %s %d" % (plugin['uri'], instance_id))
                if plugin['bypassed']:
                    self.send_notmodified("bypass %d 1" % (instance_id,))
                if -1 not in plugin['bypassCC']:
                    mchnnl, mctrl = plugin['bypassCC']
                    self.send_notmodified("midi_map %d :bypass %i %i 0.0 1.0" % (instance_id, mchnnl, mctrl))
                if plugin['preset']:
                    self.send_notmodified("preset_load %d %s" % (instance_id, plugin['preset']))

            for symbol, value in plugin['ports'].items():
                websocket.write_message("param_set %s %s %f" % (plugin['instance'], symbol, value))

                if crashed:
                    self.send_notmodified("param_set %d %s %f" % (instance_id, symbol, value))

            for symbol, value in plugin['outputs'].items():
                if value is None:
                    continue
                websocket.write_message("output_set %s %s %f" % (plugin['instance'], symbol, value))

                if crashed:
                    self.send_notmodified("monitor_output %d %s" % (instance_id, symbol))

            for symbol, data in plugin['midiCCs'].items():
                mchnnl, mctrl, minimum, maximum = data

                if -1 in (mchnnl, mctrl):
                    continue
                # don't address "bad" ports
                if symbol in plugin['badports']:
                    continue

                websocket.write_message("midi_map %s %s %i %i %f %f" % (plugin['instance'], symbol,
                                                                        mchnnl, mctrl,
                                                                        minimum, maximum))

                if crashed:
                    self.send_notmodified("midi_map %d %s %i %i %f %f" % (instance_id, symbol,
                                                                          mchnnl, mctrl, minimum, maximum))

        for port_from, port_to in self.connections:
            websocket.write_message("connect %s %s" % (port_from, port_to))

            if crashed:
                self.send_notmodified("connect %s %s" % (self._fix_host_connection_port(port_from),
                                                         self._fix_host_connection_port(port_to)))

        websocket.write_message("loading_end")

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - add & remove bundles

    def add_bundle(self, bundlepath, callback):
        if is_bundle_loaded(bundlepath):
            print("SKIPPED add_bundle, already in world")
            callback((False, "Bundle already loaded"))
            return

        def host_callback(ok):
            plugins = add_bundle_to_lilv_world(bundlepath)
            callback((True, plugins))

        self.send_notmodified("bundle_add \"%s\"" % bundlepath.replace('"','\\"'), host_callback, datatype='boolean')

    def remove_bundle(self, bundlepath, isPluginBundle, callback):
        if not is_bundle_loaded(bundlepath):
            print("SKIPPED remove_bundle, not in world")
            callback((False, "Bundle not loaded"))
            return

        if isPluginBundle and len(self.plugins) > 0:
            plugins = list_plugins_in_bundle(bundlepath)

            for plugin in self.plugins.values():
                if plugin['uri'] in plugins:
                    callback((False, "Plugin is currently in use, cannot remove"))
                    return

        def host_callback(ok):
            plugins = remove_bundle_from_lilv_world(bundlepath)
            callback((True, plugins))

        self.send_notmodified("bundle_remove \"%s\"" % bundlepath.replace('"','\\"'), host_callback, datatype='boolean')

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - reset, add, remove

    def reset(self, callback):
        def host_callback(ok):
            self.msg_callback("remove :all")
            callback(ok)

        self.bank_id = 0
        self.plugins = {}
        self.connections = []
        self.mapper.clear()
        self._init_addressings()

        self.pedalboard_empty    = True
        self.pedalboard_modified = False
        self.pedalboard_name     = ""
        self.pedalboard_path     = ""
        self.pedalboard_size     = [0,0]
        self.pedalboard_presets  = []

        save_last_bank_and_pedalboard(0, "")
        self.send_notmodified("remove -1", host_callback, datatype='boolean')

    def add_plugin(self, instance, uri, x, y, callback):
        self.pedalboard_presets = []

        instance_id = self.mapper.get_id(instance)

        def host_callback(resp):
            if resp < 0:
                callback(False)
                return
            bypassed = False

            allports = get_plugin_control_inputs_and_monitored_outputs(uri)
            badports = []
            valports = {}

            enabled_symbol = None
            freewheel_symbol = None

            for port in allports['inputs']:
                symbol = port['symbol']
                valports[symbol] = port['ranges']['default']

                # skip notOnGUI controls
                if "notOnGUI" in port['properties']:
                    badports.append(symbol)

                # skip special designated controls
                elif port['designation'] == "http://lv2plug.in/ns/lv2core#enabled":
                    enabled_symbol = symbol
                    badports.append(symbol)
                    valports[symbol] = 0.0 if bypassed else 1.0

                elif port['designation'] == "http://lv2plug.in/ns/lv2core#freeWheeling":
                    freewheel_symbol = symbol
                    badports.append(symbol)
                    valports[symbol] = 0.0

            self.plugins[instance_id] = {
                "instance"    : instance,
                "uri"         : uri,
                "bypassed"    : bypassed,
                "bypassCC"    : (-1,-1),
                "x"           : x,
                "y"           : y,
                "addressings" : {}, # symbol: addressing
                "midiCCs"     : dict((p['symbol'], (-1,-1,0.0,1.0)) for p in allports['inputs']),
                "ports"       : valports,
                "badports"    : badports,
                "designations": (enabled_symbol, freewheel_symbol),
                "outputs"     : dict((symbol, None) for symbol in allports['monitoredOutputs']),
                "preset"      : "",
                "mapPresets"  : []
            }

            for output in allports['monitoredOutputs']:
                self.send_notmodified("monitor_output %d %s" % (instance_id, output))

            callback(True)
            self.msg_callback("add %s %s %.1f %.1f %d" % (instance, uri, x, y, int(bypassed)))

        self.send_modified("add %s %d" % (uri, instance_id), host_callback, datatype='int')

    @gen.coroutine
    def remove_plugin(self, instance, callback):
        self.pedalboard_presets = []

        instance_id = self.mapper.get_id_without_creating(instance)

        try:
            data = self.plugins.pop(instance_id)
        except KeyError:
            callback(False)
            return

        used_actuators = []
        for symbol in [symbol for symbol in data['addressings'].keys()]:
            actuator_uri = self._unaddress(data, symbol)

            if actuator_uri is not None and actuator_uri not in used_actuators:
                used_actuators.append(actuator_uri)

        for actuator_uri in used_actuators:
            actuator_hw = self._uri2hw_map[actuator_uri]
            yield gen.Task(self._address_next, actuator_hw)

        def host_callback(ok):
            callback(ok)
            removed_connections = []
            for ports in self.connections:
                if ports[0].rsplit("/",1)[0] == instance or ports[1].rsplit("/",1)[0] == instance:
                    removed_connections.append(ports)
            for ports in removed_connections:
                self.connections.remove(ports)
                self.msg_callback("disconnect %s %s" % (ports[0], ports[1]))

            self.msg_callback("remove %s" % (instance))

        def hmi_callback(ok):
            self.send_modified("remove %d" % instance_id, host_callback, datatype='boolean')

        if self.hmi.initialized:
            self.hmi.control_rm(instance_id, ":all", hmi_callback)
        else:
            hmi_callback(True)

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - plugin values

    def bypass(self, instance, bypassed, callback):
        instance_id = self.mapper.get_id_without_creating(instance)

        self.plugins[instance_id]['bypassed'] = bypassed
        self.send_modified("bypass %d %d" % (instance_id, int(bypassed)), callback, datatype='boolean')

        enabled_symbol = self.plugins[instance_id]['designations'][0]
        if enabled_symbol is None:
            return

        value = 0.0 if bypassed else 1.0
        self.plugins[instance_id]['ports'][enabled_symbol] = value
        self.send_modified("param_set %d %s %f" % (instance_id, enabled_symbol, value), callback, datatype='boolean')

    def param_set(self, port, value, callback):
        instance, symbol = port.rsplit("/", 1)
        instance_id = self.mapper.get_id_without_creating(instance)

        if symbol in self.plugins[instance_id]['designations']:
            print("ERROR: Trying to modify a specially designated port '%s', stop!" % symbol)
            return

        self.plugins[instance_id]['ports'][symbol] = value
        self.send_modified("param_set %d %s %f" % (instance_id, symbol, value), callback, datatype='boolean')

    def preset_load(self, instance, uri, callback):
        instance_id = self.mapper.get_id_without_creating(instance)

        @gen.coroutine
        def preset_callback(state):
            if not state:
                callback(False)
                return

            plugin = self.plugins[instance_id]

            plugin['preset'] = uri
            self.msg_callback("preset %s %s" % (instance, uri))

            portValues = get_state_port_values(state)
            plugin['ports'].update(portValues)

            badports = plugin['badports']
            used_actuators = []

            enabled_symbol, freewheel_symbol = plugin['designations']

            for symbol, value in plugin['ports'].items():
                if symbol == enabled_symbol:
                    value = 0.0 if plugin['bypassed'] else 1.0
                elif symbol == freewheel_symbol:
                    value = 0.0

                self.msg_callback("param_set %s %s %f" % (instance, symbol, value))

                addressing = plugin['addressings'].get(symbol, None)
                if addressing is not None and addressing['actuator_uri'] not in used_actuators:
                    used_actuators.append(addressing['actuator_uri'])

            for actuator_uri in used_actuators:
                yield gen.Task(self._addressing_load, actuator_uri, skipPresets=True)

            callback(True)

        def host_callback(ok):
            if not ok:
                callback(False)
                return
            self.send_notmodified("preset_show %s" % uri, preset_callback, datatype='string')

        self.send_modified("preset_load %d %s" % (instance_id, uri), host_callback, datatype='boolean')

    def preset_save_new(self, instance, name, callback):
        instance_id  = self.mapper.get_id_without_creating(instance)
        plugin_uri   = self.plugins[instance_id]['uri']
        symbolname   = symbolify(name)[:32]
        presetbundle = os.path.expanduser("~/.lv2/%s-%s.lv2") % (instance.replace("/graph/","",1), symbolname)

        if os.path.exists(presetbundle):
            # if presetbundle already exists, generate a new random bundle path
            while True:
                presetbundle = os.path.expanduser("~/.lv2/%s-%s-%i.lv2" % (instance.replace("/graph/","",1), symbolname, randint(1,99999)))
                if os.path.exists(presetbundle):
                    continue
                break

        def add_bundle_callback(ok):
            # done
            preseturi = "file://%s.ttl" % os.path.join(presetbundle, symbolname)
            self.plugins[instance_id]['preset'] = preseturi

            callback({
                'ok'    : True,
                'bundle': presetbundle,
                'uri'   : preseturi
            })
            print("uri saved as '%s'" % preseturi)

        def host_callback(ok):
            if not ok:
                callback({
                    'ok': False,
                })
                return
            rescan_plugin_presets(plugin_uri)
            self.add_bundle(presetbundle, add_bundle_callback)

        self.send_notmodified("preset_save %d \"%s\" %s %s.ttl" % (instance_id,
                                                                   name.replace('"','\\"'),
                                                                   presetbundle,
                                                                   symbolname), host_callback, datatype='boolean')

    def preset_save_replace(self, instance, uri, bundlepath, name, callback):
        instance_id = self.mapper.get_id_without_creating(instance)
        plugin_uri  = self.plugins[instance_id]['uri']
        symbolname  = symbolify(name)[:32]

        if self.plugins[instance_id]['preset'] != uri or not os.path.exists(bundlepath):
            callback({
                'ok': False,
            })
            return

        def add_bundle_callback(ok):
            preseturi = "file://%s.ttl" % os.path.join(bundlepath, symbolname)
            self.plugins[instance_id]['preset'] = preseturi
            callback({
                'ok'    : True,
                'bundle': bundlepath,
                'uri'   : preseturi
            })
            print("uri saved as '%s'" % preseturi)

        def host_callback(ok):
            if not ok:
                callback({
                    'ok': False,
                })
                return
            self.add_bundle(bundlepath, add_bundle_callback)

        def start(ok):
            rmtree(bundlepath)
            rescan_plugin_presets(plugin_uri)
            self.plugins[instance_id]['preset'] = ""
            self.send_notmodified("preset_save %d \"%s\" %s %s.ttl" % (instance_id,
                                                                       name.replace('"','\\"'),
                                                                       bundlepath,
                                                                       symbolname), host_callback, datatype='boolean')

        self.remove_bundle(bundlepath, False, start)

    def preset_delete(self, instance, uri, bundlepath, callback):
        instance_id = self.mapper.get_id_without_creating(instance)
        plugin_uri  = self.plugins[instance_id]['uri']

        if self.plugins[instance_id]['preset'] != uri or not os.path.exists(bundlepath):
            callback(False)
            return

        def start(ok):
            rmtree(bundlepath)
            rescan_plugin_presets(plugin_uri)
            self.plugins[instance_id]['preset'] = ""
            self.msg_callback("preset %s null" % instance)
            callback(True)

        self.remove_bundle(bundlepath, False, start)

    def set_position(self, instance, x, y):
        instance_id = self.mapper.get_id_without_creating(instance)

        self.plugins[instance_id]['x'] = x
        self.plugins[instance_id]['y'] = y

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - connections

    def _fix_host_connection_port(self, port):
        data = port.split("/")

        if len(data) == 3:
            if data[2] == "serial_midi_in":
                return "ttymidi:MIDI_in"
            if data[2] == "serial_midi_out":
                return "ttymidi:MIDI_out"
            if data[2].startswith("playback_"):
                num = data[2].replace("playback_","",1)
                if num in ("1", "2"):
                    return "mod-host:monitor-in_" + num
            if data[2].startswith("nooice_capture_"):
                num = data[2].replace("nooice_capture_","",1)
                return "nooice%s:nooice_capture_%s" % (num, num)
            return "system:%s" % data[2]

        instance    = "/graph/%s" % data[2]
        portsymbol  = data[3]
        instance_id = self.mapper.get_id_without_creating(instance)
        return "effect_%d:%s" % (instance_id, portsymbol)

    def connect(self, port_from, port_to, callback):
        self.pedalboard_presets = []

        if (port_from, port_to) in self.connections:
            print("NOTE: Requested connection already exists")
            callback(True)
            return

        def host_callback(ok):
            callback(ok)
            if ok:
                self.connections.append((port_from, port_to))
                self.msg_callback("connect %s %s" % (port_from, port_to))
            else:
                print("ERROR: backend failed to connect ports: '%s' => '%s'" % (port_from, port_to))

        self.send_modified("connect %s %s" % (self._fix_host_connection_port(port_from),
                                              self._fix_host_connection_port(port_to)),
                           host_callback, datatype='boolean')

    def disconnect(self, port_from, port_to, callback):
        self.pedalboard_presets = []

        def host_callback(ok):
            # always return true. disconnect failures are not fatal, but still print error for debugging
            callback(True)
            self.msg_callback("disconnect %s %s" % (port_from, port_to))

            if not ok:
                print("ERROR: disconnect '%s' => '%s' failed" % (port_from, port_to))
                return

            self.pedalboard_modified = True

            try:
                self.connections.remove((port_from, port_to))
            except:
                print("Requested '%s' => '%s' connection doesn't exist" % (port_from, port_to))

        if len(self.connections) == 0:
            return host_callback(False)

        # If the plugin or port don't exist, assume disconnected
        try:
            port_from_2 = self._fix_host_connection_port(port_from)
        except:
            print("Requested '%s' source port doesn't exist, assume disconnected" % port_from)
            return host_callback(True)

        try:
            port_to_2 = self._fix_host_connection_port(port_to)
        except:
            print("Requested '%s' target port doesn't exist, assume disconnected" % port_to)
            return host_callback(True)

        host_callback(disconnect_jack_ports(port_from_2, port_to_2))

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - load & save

    def load(self, bundlepath, isDefault=False):
        pb = get_pedalboard_info(bundlepath)

        self.msg_callback("loading_start %i 0" % int(isDefault))
        self.msg_callback("size %d %d" % (pb['width'],pb['height']))

        # MIDI Devices might change port names at anytime
        # To properly restore MIDI HW connections we need to map the "old" port names (from project)
        mappedOldMidiIns   = dict((p['symbol'], p['name']) for p in pb['hardware']['midi_ins'])
        mappedOldMidiOuts  = dict((p['symbol'], p['name']) for p in pb['hardware']['midi_outs'])
        mappedOldMidiOuts2 = dict((p['name'], p['symbol']) for p in pb['hardware']['midi_outs'])
        mappedNewMidiIns   = OrderedDict((get_jack_port_alias(p).split("-",5)[-1].replace("-"," ").replace(";","."),
                                          p.split(":",1)[-1]) for p in get_jack_hardware_ports(False, False))
        mappedNewMidiOuts  = OrderedDict((get_jack_port_alias(p).split("-",5)[-1].replace("-"," ").replace(";","."),
                                          p.split(":",1)[-1]) for p in get_jack_hardware_ports(False, True))

        curmidisymbols = []
        for port_symbol, port_alias, _ in self.midiports:
            if ";" in port_symbol:
                ports = port_symbol.split(";", 1)
                curmidisymbols.append(ports[0].split(":",1)[-1])
                curmidisymbols.append(ports[1].split(":",1)[-1])
            else:
                curmidisymbols.append(port_symbol.split(":",1)[-1])

        # register devices
        index = 0
        for name, symbol in mappedNewMidiIns.items():
            index += 1
            if name not in mappedOldMidiIns.values():
                continue
            if symbol in curmidisymbols:
                continue
            self.msg_callback("add_hw_port /graph/%s midi 0 %s %i" % (symbol, name.replace(" ","_"), index))

            if name in mappedNewMidiOuts.keys():
                outsymbol    = mappedNewMidiOuts[name]
                storedtitle  = name+";"+name
                storedsymbol = "system:%s;system:%s" % (symbol, outsymbol)
            else:
                storedtitle = name
                if symbol.startswith("nooice_capture_"):
                    num = symbol.replace("nooice_capture_","",1)
                    storedsymbol = "nooice%s:nooice_capture_%s" % (num, num)
                else:
                    storedsymbol = "system:" + symbol
            curmidisymbols.append(symbol)
            self.midiports.append([storedsymbol, storedtitle, []])

        # try to find old devices that are not available right now
        for symbol, name in mappedOldMidiIns.items():
            if symbol.split(":",1)[-1] in curmidisymbols:
                continue
            if name in mappedNewMidiOuts.keys():
                continue
            # found it
            if name in mappedOldMidiOuts2.keys():
                outsymbol   = mappedOldMidiOuts2[name]
                storedtitle = name+";"+name
                if ":" in symbol:
                    storedsymbol = "%s;%s" % (symbol, outsymbol)
                else:
                    storedsymbol = "system:%s;system:%s" % (symbol, outsymbol)
            else:
                storedtitle = name
                if ":" in symbol:
                    storedsymbol = symbol
                else:
                    storedsymbol = "system:" + symbol
            self.midiports.append([storedsymbol, storedtitle, []])

        index = 0
        for name, symbol in mappedNewMidiOuts.items():
            index += 1
            if name not in mappedOldMidiOuts.values():
                continue
            if symbol in curmidisymbols:
                continue
            self.msg_callback("add_hw_port /graph/%s midi 1 %s %i" % (symbol, name.replace(" ","_"), index))

        def_pb_preset = {
            "name": "Default",
            "data": {},
        }

        for p in pb['plugins']:
            allports = get_plugin_control_inputs_and_monitored_outputs(p['uri'])

            if 'error' in allports.keys() and allports['error']:
                continue

            instance    = "/graph/%s" % p['instance']
            instance_id = self.mapper.get_id(instance)

            badports = []
            valports = {}
            ranges   = {}

            enabled_symbol = None
            freewheel_symbol = None

            for port in allports['inputs']:
                symbol = port['symbol']
                valports[symbol] = port['ranges']['default']
                ranges[symbol] = (port['ranges']['minimum'], port['ranges']['maximum'])

                # skip notOnGUI controls
                if "notOnGUI" in port['properties']:
                    badports.append(symbol)

                # skip special designated controls
                elif port['designation'] == "http://lv2plug.in/ns/lv2core#enabled":
                    enabled_symbol = symbol
                    badports.append(symbol)
                    valports[symbol] = 0.0 if p['bypassed'] else 1.0

                elif port['designation'] == "http://lv2plug.in/ns/lv2core#freeWheeling":
                    freewheel_symbol = symbol
                    badports.append(symbol)
                    valports[symbol] = 0.0

            self.plugins[instance_id] = {
                "instance"    : instance,
                "uri"         : p['uri'],
                "bypassed"    : p['bypassed'],
                "bypassCC"    : (p['bypassCC']['channel'], p['bypassCC']['control']),
                "x"           : p['x'],
                "y"           : p['y'],
                "addressings" : {}, # symbol: addressing
                "midiCCs"     : dict((p['symbol'], (-1,-1,0.0,1.0)) for p in allports['inputs']),
                "ports"       : valports,
                "badports"    : badports,
                "designations": (enabled_symbol, freewheel_symbol),
                "outputs"     : dict((symbol, None) for symbol in allports['monitoredOutputs']),
                "preset"      : p['preset'],
                "mapPresets"  : []
            }

            def_pb_preset['data'][instance] = {
                "bypassed": p['bypassed'],
                "ports"   : valports,
                "preset"  : p['preset'],
            }

            self.send_notmodified("add %s %d" % (p['uri'], instance_id))

            if p['bypassed']:
                self.send_notmodified("bypass %d 1" % (instance_id,))

            self.msg_callback("add %s %s %.1f %.1f %d" % (instance, p['uri'], p['x'], p['y'], int(p['bypassed'])))

            if p['bypassCC']['channel'] >= 0 and p['bypassCC']['control'] >= 0:
                self.send_notmodified("midi_map %d :bypass %i %i 0.0 1.0" % (instance_id, p['bypassCC']['channel'],
                                                                                          p['bypassCC']['control']))
                self.msg_callback("midi_map %s :bypass %i %i 0.0 1.0" % (instance, p['bypassCC']['channel'],
                                                                                   p['bypassCC']['control']))

            if p['preset']:
                self.send_notmodified("preset_load %d %s" % (instance_id, p['preset']))
                self.msg_callback("preset %s %s" % (instance, p['preset']))

            for port in p['ports']:
                symbol = port['symbol']
                value  = port['value']
                mchnnl = port['midiCC']['channel']
                mctrl  = port['midiCC']['control']

                if port['midiCC']['hasRanges']:
                    minimum = port['midiCC']['minimum']
                    maximum = port['midiCC']['maximum']
                else:
                    minimum, maximum = ranges[symbol]

                self.plugins[instance_id]['ports'][symbol] = value

                self.send_notmodified("param_set %d %s %f" % (instance_id, symbol, value))
                self.msg_callback("param_set %s %s %f" % (instance, symbol, value))

                # don't address "bad" ports
                if symbol in badports:
                    continue

                if mchnnl >= 0 and mctrl >= 0:
                    self.plugins[instance_id]['midiCCs'][symbol] = (mchnnl, mctrl, minimum, maximum)
                    self.send_notmodified("midi_map %d %s %i %i %f %f" % (instance_id, symbol,
                                                                          mchnnl, mctrl, minimum, maximum))
                    self.msg_callback("midi_map %s %s %i %i %f %f" % (instance, symbol,
                                                                      mchnnl, mctrl, minimum, maximum))

            for output in allports['monitoredOutputs']:
                self.send_notmodified("monitor_output %d %s" % (instance_id, output))

        for c in pb['connections']:
            doConnectionNow = True
            aliasname1 = aliasname2 = None

            if c['source'] in mappedOldMidiIns.keys():
                aliasname1 = mappedOldMidiIns[c['source']]
                try:
                    portname = mappedNewMidiIns[aliasname1]
                except:
                    doConnectionNow = False
                else:
                    c['source'] = portname

            if c['target'] in mappedOldMidiOuts.keys():
                aliasname2 = mappedOldMidiOuts[c['target']]
                try:
                    portname = mappedNewMidiOuts[aliasname2]
                except:
                    doConnectionNow = False
                else:
                    c['target'] = portname

            port_from = "/graph/%s" % c['source']
            port_to   = "/graph/%s" % c['target']

            if doConnectionNow:
                try:
                    port_from_2 = self._fix_host_connection_port(port_from)
                    port_to_2   = self._fix_host_connection_port(port_to)
                except:
                    continue
                self.send_notmodified("connect %s %s" % (port_from_2, port_to_2))
                self.connections.append((port_from, port_to))
                self.msg_callback("connect %s %s" % (port_from, port_to))

            elif aliasname1 is not None or aliasname2 is not None:
                for port_symbol, port_alias, port_conns in self.midiports:
                    port_alias = port_alias.split(";",1) if ";" in port_alias else [port_alias]
                    if aliasname1 in port_alias or aliasname2 in port_alias:
                        port_conns.append((port_from, port_to))

        self.pedalboard_presets = [def_pb_preset]

        if os.path.exists(os.path.join(bundlepath, "presets.json")):
            with open(os.path.join(bundlepath, "presets.json")) as fh:
                more_pb_presets = json.loads(fh)
            if isinstance(more_pb_presets, list) and len(more_pb_presets) != 0:
                self.pedalboard_presets += more_pb_presets

        if self.hmi.initialized:
            self._load_addressings(bundlepath)

        self.msg_callback("loading_end")

        if isDefault:
            self.pedalboard_empty    = True
            self.pedalboard_modified = False
            self.pedalboard_name     = ""
            self.pedalboard_path     = ""
            self.pedalboard_size     = [0,0]
            #save_last_bank_and_pedalboard(0, "")
        else:
            self.pedalboard_empty    = False
            self.pedalboard_modified = False
            self.pedalboard_name     = pb['title']
            self.pedalboard_path     = bundlepath
            self.pedalboard_size     = [pb['width'],pb['height']]

            if bundlepath.startswith(LV2_PEDALBOARDS_DIR):
                save_last_bank_and_pedalboard(self.bank_id, bundlepath)
            else:
                save_last_bank_and_pedalboard(0, "")

        return self.pedalboard_name

    def save(self, title, asNew):
        titlesym = symbolify(title)[:16]

        # Save over existing bundlepath
        if self.pedalboard_path and os.path.exists(self.pedalboard_path) and os.path.isdir(self.pedalboard_path) and \
            self.pedalboard_path.startswith(LV2_PEDALBOARDS_DIR) and not asNew:
            bundlepath = self.pedalboard_path

        # Save new
        else:
            lv2path = os.path.expanduser("~/.pedalboards/")
            trypath = os.path.join(lv2path, "%s.pedalboard" % titlesym)

            # if trypath already exists, generate a random bundlepath based on title
            if os.path.exists(trypath):
                while True:
                    trypath = os.path.join(lv2path, "%s-%i.pedalboard" % (titlesym, randint(1,99999)))
                    if os.path.exists(trypath):
                        continue
                    bundlepath = trypath
                    break

            # trypath doesn't exist yet, use it
            else:
                bundlepath = trypath

                # just in case..
                if not os.path.exists(lv2path):
                    os.mkdir(lv2path)

            os.mkdir(bundlepath)
            self.pedalboard_path = bundlepath

        # save
        self.pedalboard_name     = title
        self.pedalboard_empty    = False
        self.pedalboard_modified = False
        self.save_state_to_ttl(bundlepath, title, titlesym)

        save_last_bank_and_pedalboard(0, bundlepath)

        return bundlepath

    def save_state_to_ttl(self, bundlepath, title, titlesym):
        self.save_state_manifest(bundlepath, titlesym)
        self.save_state_addressings(bundlepath)
        self.save_state_presets(bundlepath)
        self.save_state_mainfile(bundlepath, title, titlesym)

    def save_state_manifest(self, bundlepath, titlesym):
        # Write manifest.ttl
        with open(os.path.join(bundlepath, "manifest.ttl"), 'w') as fh:
            fh.write("""\
@prefix ingen: <http://drobilla.net/ns/ingen#> .
@prefix lv2:   <http://lv2plug.in/ns/lv2core#> .
@prefix pedal: <http://moddevices.com/ns/modpedal#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .

<%s.ttl>
    lv2:prototype ingen:GraphPrototype ;
    a lv2:Plugin ,
        ingen:Graph ,
        pedal:Pedalboard ;
    rdfs:seeAlso <%s.ttl> .
""" % (titlesym, titlesym))

    def save_state_addressings(self, bundlepath):
        # Write addressings.json
        addressings = self.get_addressings()

        with open(os.path.join(bundlepath, "addressings.json"), 'w') as fh:
            json.dump(addressings, fh)

    def save_state_presets(self, bundlepath):
        # Write presets.json
        presets_path = os.path.join(bundlepath, "presets.json")

        if len(self.pedalboard_presets) > 1:
            with open(presets_path, 'w') as fh:
                json.dump(self.pedalboard_presets[1:], fh)

        elif os.path.exists(presets_path):
            os.remove(presets_path)

    def save_state_mainfile(self, bundlepath, title, titlesym):
        # Create list of midi in/out ports
        midiportsIn   = []
        midiportsOut  = []
        midiportAlias = {}

        for port_symbol, port_alias, _ in self.midiports:
            if ";" in port_symbol:
                inp, outp = port_symbol.split(";",1)
                midiportsIn.append(inp)
                midiportsOut.append(outp)
                title_in, title_out = port_alias.split(";",1)
                midiportAlias[inp]  = title_in
                midiportAlias[outp] = title_out
            else:
                midiportsIn.append(port_symbol)
                midiportAlias[port_symbol] = port_alias

        # Arcs (connections)
        arcs = ""
        index = 0
        for port_from, port_to in self.connections:
            index += 1
            arcs += """
_:b%i
    ingen:tail <%s> ;
    ingen:head <%s> .
""" % (index, port_from.replace("/graph/","",1), port_to.replace("/graph/","",1))

        # Blocks (plugins)
        blocks = ""
        for plugin in self.plugins.values():
            info = get_plugin_info(plugin['uri'])
            instance = plugin['instance'].replace("/graph/","",1)
            blocks += """
<%s>
    ingen:canvasX %.1f ;
    ingen:canvasY %.1f ;
    ingen:enabled %s ;
    ingen:polyphonic false ;
    lv2:microVersion %i ;
    lv2:minorVersion %i ;
    mod:builderVersion %i ;
    mod:releaseNumber %i ;
    lv2:port <%s> ;
    lv2:prototype <%s> ;
    pedal:preset <%s> ;
    a ingen:Block .
""" % (instance, plugin['x'], plugin['y'], "false" if plugin['bypassed'] else "true",
       info['microVersion'], info['minorVersion'], info['builder'], info['release'],
       "> ,\n             <".join(tuple("%s/%s" % (instance, port['symbol']) for port in (info['ports']['audio']['input']+
                                                                                          info['ports']['audio']['output']+
                                                                                          info['ports']['control']['input']+
                                                                                          info['ports']['control']['output']+
                                                                                          info['ports']['cv']['input']+
                                                                                          info['ports']['cv']['output']+
                                                                                          info['ports']['midi']['input']+
                                                                                          info['ports']['midi']['output']+
                                                                                          [{'symbol': ":bypass"}]))),
       plugin['uri'],
       plugin['preset'])

            # audio input
            for port in info['ports']['audio']['input']:
                blocks += """
<%s/%s>
    a lv2:AudioPort ,
        lv2:InputPort .
""" % (instance, port['symbol'])

            # audio output
            for port in info['ports']['audio']['input']:
                blocks += """
<%s/%s>
    a lv2:AudioPort ,
        lv2:OutputPort .
""" % (instance, port['symbol'])

            # cv input
            for port in info['ports']['cv']['input']:
                blocks += """
<%s/%s>
    a lv2:CVPort ,
        lv2:InputPort .
""" % (instance, port['symbol'])

            # cv output
            for port in info['ports']['cv']['output']:
                blocks += """
<%s/%s>
    a lv2:CVPort ,
        lv2:OutputPort .
""" % (instance, port['symbol'])

            # midi input
            for port in info['ports']['midi']['input']:
                blocks += """
<%s/%s>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    a atom:AtomPort ,
        lv2:InputPort .
""" % (instance, port['symbol'])

            # midi output
            for port in info['ports']['midi']['output']:
                blocks += """
<%s/%s>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    a atom:AtomPort ,
        lv2:OutputPort .
""" % (instance, port['symbol'])

            # control input, save values
            for symbol, value in plugin['ports'].items():
                blocks += """
<%s/%s>
    ingen:value %f ;%s
    a lv2:ControlPort ,
        lv2:InputPort .
""" % (instance, symbol, value,
       ("""
    midi:binding [
        midi:channel %i ;
        midi:controllerNumber %i ;
        lv2:minimum %f ;
        lv2:maximum %f ;
        a midi:Controller ;
    ] ;""" % plugin['midiCCs'][symbol]) if -1 not in plugin['midiCCs'][symbol] else "")

            # control output
            for port in info['ports']['control']['output']:
                blocks += """
<%s/%s>
    a lv2:ControlPort ,
        lv2:OutputPort .
""" % (instance, port['symbol'])

            blocks += """
<%s/:bypass>
    ingen:value %i ;%s
    a lv2:ControlPort ,
        lv2:InputPort .
""" % (instance, 1 if plugin['bypassed'] else 0,
       ("""
    midi:binding [
        midi:channel %i ;
        midi:controllerNumber %i ;
        a midi:Controller ;
    ] ;""" % plugin['bypassCC']) if -1 not in plugin['bypassCC'] else "")

        # Ports
        ports = """
<control_in>
    atom:bufferType atom:Sequence ;
    lv2:index 0 ;
    lv2:name "Control In" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "control_in" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:InputPort .

<control_out>
    atom:bufferType atom:Sequence ;
    lv2:index 1 ;
    lv2:name "Control Out" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "control_out" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:OutputPort .
"""
        index = 1

        # Ports (Audio In)
        for port in self.audioportsIn:
            index += 1
            ports += """
<%s>
    lv2:index %i ;
    lv2:name "%s" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "%s" ;
    a lv2:AudioPort ,
        lv2:InputPort .
""" % (port, index, port.title().replace("_"," "), port)

        # Ports (Audio Out)
        for port in self.audioportsOut:
            index += 1
            ports += """
<%s>
    lv2:index %i ;
    lv2:name "%s" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "%s" ;
    a lv2:AudioPort ,
        lv2:OutputPort .
""" % (port, index, port.title().replace("_"," "), port)

        # Ports (MIDI In)
        for port in midiportsIn:
            sname  = port.replace("system:","",1)
            index += 1
            ports += """
<%s>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    lv2:index %i ;
    lv2:name "%s" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "%s" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:InputPort .
""" % (sname, index, midiportAlias[port], sname)

        # Ports (MIDI Out)
        for port in midiportsOut:
            sname  = port.replace("system:","",1)
            index += 1
            ports += """
<%s>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    lv2:index %i ;
    lv2:name "%s" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "%s" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:OutputPort .
""" % (sname, index, midiportAlias[port], sname)

        # Serial MIDI In
        if self.hasSerialMidiIn:
            index += 1
            ports += """
<serial_midi_in>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    lv2:index %i ;
    lv2:name "Serial MIDI In" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "serial_midi_in" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:InputPort .
""" % index

        # Serial MIDI Out
        if self.hasSerialMidiOut:
            index += 1
            ports += """
<serial_midi_out>
    atom:bufferType atom:Sequence ;
    atom:supports midi:MidiEvent ;
    lv2:index %i ;
    lv2:name "Serial MIDI In" ;
    lv2:portProperty lv2:connectionOptional ;
    lv2:symbol "serial_midi_out" ;
    <http://lv2plug.in/ns/ext/resize-port#minimumSize> 4096 ;
    a atom:AtomPort ,
        lv2:OutputPort .
""" % index

        # Write the main pedalboard file
        pbdata = """\
@prefix atom:  <http://lv2plug.in/ns/ext/atom#> .
@prefix doap:  <http://usefulinc.com/ns/doap#> .
@prefix ingen: <http://drobilla.net/ns/ingen#> .
@prefix lv2:   <http://lv2plug.in/ns/lv2core#> .
@prefix midi:  <http://lv2plug.in/ns/ext/midi#> .
@prefix mod:   <http://moddevices.com/ns/mod#> .
@prefix pedal: <http://moddevices.com/ns/modpedal#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
%s%s%s
<>
    doap:name "%s" ;
    pedal:width %i ;
    pedal:height %i ;
    pedal:addressings <addressings.json> ;
    pedal:screenshot <screenshot.png> ;
    pedal:thumbnail <thumbnail.png> ;
    ingen:polyphony 1 ;
""" % (arcs, blocks, ports, title.replace('"','\\"'), self.pedalboard_size[0], self.pedalboard_size[1])

        # Arcs (connections)
        if len(self.connections) > 0:
            pbdata += "    ingen:arc _:b%s ;\n" % (" ,\n              _:b".join(tuple(str(i+1) for i in range(len(self.connections)))))

        # Blocks (plugins)
        if len(self.plugins) > 0:
            pbdata += "    ingen:block <%s> ;\n" % ("> ,\n                <".join(tuple(p['instance'].replace("/graph/","",1) for p in self.plugins.values())))

        # Ports
        portsyms = ["control_in","control_out"]
        if self.hasSerialMidiIn:
            portsyms.append("serial_midi_in")
        if self.hasSerialMidiOut:
            portsyms.append("serial_midi_out")
        portsyms += [p.replace("system:","",1) for p in midiportsIn ]
        portsyms += [p.replace("system:","",1) for p in midiportsOut]
        pbdata += "    lv2:port <%s> ;\n" % ("> ,\n             <".join(portsyms+self.audioportsIn+self.audioportsOut))

        # End
        pbdata += """\
    lv2:extensionData <http://lv2plug.in/ns/ext/state#interface> ;
    a lv2:Plugin ,
        ingen:Graph ,
        pedal:Pedalboard .
"""

        # Write the main pedalboard file
        with open(os.path.join(bundlepath, "%s.ttl" % titlesym), 'w') as fh:
            fh.write(pbdata)

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - misc

    def set_pedalboard_size(self, width, height):
        self.pedalboard_size = [width, height]

    def add_external_port(self, name, mode, typ, callback):
        # ignored
        callback(True)

    def remove_external_port(self, name, callback):
        # ignored
        callback(True)

    # -----------------------------------------------------------------------------------------------------------------
    # Host stuff - timers

    def statstimer_callback(self):
        data = get_jack_data()
        self.msg_callback("stats %0.1f %i" % (data['cpuLoad'], data['xruns']))

    def get_free_memory_value(self):
        if not self.memfile:
            return "??"

        self.memfile.seek(self.memfseek)
        memfree = float(int(self.memfile.readline().replace("MemFree:","",1).replace("kB","",1).strip()))

        # skip 'MemAvailable'
        if self.memfskip: self.memfile.readline()

        memfree += float(int(self.memfile.readline().replace("Buffers:","",1).replace("kB","",1).strip()))
        memfree += float(int(self.memfile.readline().replace("Cached:" ,"",1).replace("kB","",1).strip()))

        return "%0.1f" % ((self.memtotal-memfree)/self.memtotal*100.0)

    def memtimer_callback(self):
        self.msg_callback("mem_load " + self.get_free_memory_value())

    # -----------------------------------------------------------------------------------------------------------------
    # Addressing (public stuff)

    def address(self, instance, port, actuator_uri, label, minimum, maximum, value, steps, callback, skipLoad=False):
        instance_id = self.mapper.get_id(instance)

        pluginData = self.plugins.get(instance_id, None)
        if pluginData is None:
            print("ERROR: Trying to address non-existing plugin instance %i: '%s'" % (instance_id, instance))
            callback(False)
            return

        old_actuator_uri = self._unaddress(pluginData, port)

        # we're addressing to HMI
        if actuator_uri and actuator_uri not in (kNullAddressURI, kMidiLearnURI, kMidiUnmapURI) and port not in pluginData['designations']:
            options = []
            spreset = ""

            if port == ":bypass":
                ctype = ADDRESSING_CTYPE_BYPASS
                unit  = "none"

            elif port == ":presets":
                ctype = ADDRESSING_CTYPE_SCALE_POINTS|ADDRESSING_CTYPE_ENUMERATION|ADDRESSING_CTYPE_INTEGER
                unit  = "none"

                presets = get_plugin_info(pluginData["uri"])['presets']
                minimum = 0
                maximum = min(len(presets), kMaxAddressableScalepoints)

                # save preset mapping index
                pluginData['mapPresets'] = []

                # safety check
                if len(presets) == 0:
                    pluginData['preset'] = ""
                    if not skipLoad:
                        callback(False)
                    return

                handled = False

                # if no preset selected yet, we need to force one
                if not pluginData['preset']:
                    pluginData['preset'] = presets[0]["uri"]
                    handled = True
                    value = 0
                    # TODO: load preset?

                # save preset list, within a limit
                for i in range(maximum):
                    uri = presets[i]["uri"]
                    pluginData['mapPresets'].append(uri)
                    options.append((str(i), presets[i]["label"]))
                    if handled:
                        continue
                    if pluginData['preset'] == uri:
                        value = i
                        handled = True

                # check if selected preset is non-existent
                if not handled and len(presets) == maximum:
                    pluginData['mapPresets'] = []
                    pluginData['preset'] = ""
                    if not skipLoad:
                        callback(False)
                    return

                # handle case of current preset out of limits (>100)
                if pluginData['preset'] not in pluginData['mapPresets']:
                    i = value = maximum
                    maximum += 1
                    pluginData['mapPresets'].append(presets[i]["uri"])
                    options.append((str(i), presets[i]["label"]))

                spreset = pluginData['preset']
                del presets

            else:
                for port_info in get_plugin_control_inputs_and_monitored_outputs(pluginData["uri"])['inputs']:
                    if port_info["symbol"] == port:
                        break
                else:
                    print("ERROR: Trying to address non-existing control port '%s'" % (port))
                    callback(False)
                    return

                pprops = port_info["properties"]
                unit   = port_info["units"]["symbol"] if "symbol" in port_info["units"] else "none"

                if "toggled" in pprops:
                    ctype = ADDRESSING_CTYPE_TOGGLED
                elif "integer" in pprops:
                    ctype = ADDRESSING_CTYPE_INTEGER
                else:
                    ctype = ADDRESSING_CTYPE_LINEAR

                if "logarithmic" in pprops:
                    ctype |= ADDRESSING_CTYPE_LOGARITHMIC
                if "trigger" in pprops:
                    ctype |= ADDRESSING_CTYPE_TRIGGER

                if "tapTempo" in pprops and actuator_uri.startswith("/hmi/footswitch"):
                    ctype |= ADDRESSING_CTYPE_TAP_TEMPO

                # FIXME: make fw accept scalepoints without enumeration
                if len(port_info["scalePoints"]) > 0 and "enumeration" in pprops:
                    ctype |= ADDRESSING_CTYPE_SCALE_POINTS|ADDRESSING_CTYPE_ENUMERATION

                    #if len(port_info["scalePoints"]) > 1 and "enumeration" in pprops:
                        #ctype |= ADDRESSING_CTYPE_ENUMERATION

                    for scalePoint in port_info["scalePoints"]:
                        options.append((scalePoint["value"], scalePoint["label"]))

                del port_info, pprops

            addressing = {
                'actuator_uri': actuator_uri,
                'instance_id': instance_id,
                'port': port,
                'label': label,
                'type': ctype,
                'unit': unit,
                'minimum': minimum,
                'maximum': maximum,
                'steps': steps,
                'options': options,
            }
            pluginData['addressings'][port] = addressing
            self.addressings[actuator_uri]['addrs'].append(addressing)
            self.addressings[actuator_uri]['idx'] = len(self.addressings[actuator_uri]['addrs']) - 1

            if skipLoad:
                return

            def nextStepAddressing2(ok):
                self._addressing_load(actuator_uri, callback)

            def nextStepAddressing(ok):
                if spreset:
                    self.preset_load(instance, spreset, nextStepAddressing2)
                else:
                    self._addressing_load(actuator_uri, callback)

            def nextStepUnaddressing(ok):
                old_actuator_hw = self._uri2hw_map[old_actuator_uri]
                self._address_next(old_actuator_hw, nextStepAddressing)

            if old_actuator_uri is not None:
                self.hmi.control_rm(instance_id, port, nextStepUnaddressing)
            else:
                nextStepAddressing(True)
            return

        if skipLoad:
            return

        def unaddressingStep2(ok):
            # we're trying to midi-learn
            if actuator_uri == kMidiLearnURI:
                self.send_notmodified("midi_learn %i %s %f %f" % (instance_id, port,
                                                                  minimum, maximum), callback, datatype='boolean')
                return

            # we're unmapping a midi control
            if actuator_uri == kMidiUnmapURI:
                if port == ":bypass":
                    pluginData['bypassCC'] = (-1,-1)
                else:
                    pluginData['midiCCs'][port] = (-1,-1,0.0,1.0)
                self.send_modified("midi_unmap %i %s" % (instance_id, port), callback, datatype='boolean')
                return

            # nothing
            callback(True)

        def unaddressingStep1(ok):
            old_actuator_hw = self._uri2hw_map[old_actuator_uri]
            self._address_next(old_actuator_hw, unaddressingStep2)

        if old_actuator_uri is not None:
            self.hmi.control_rm(instance_id, port, unaddressingStep1)
        else:
            unaddressingStep2(True)

    def get_addressings(self):
        if len(self.addressings) == 0:
            return {}
        addressings = {}
        for uri, addressing in self.addressings.items():
            addrs = []
            for addr in addressing['addrs']:
                addrs.append({
                    'instance': self.mapper.get_instance(addr['instance_id']),
                    'port'    : addr['port'],
                    'label'   : addr['label'],
                    'minimum' : addr['minimum'],
                    'maximum' : addr['maximum'],
                    'steps'   : addr['steps'],
                })
            addressings[uri] = addrs
        return addressings

    # -----------------------------------------------------------------------------------------------------------------
    # Addressing (private stuff)

    def _init_addressings(self):
        # 'self.addressings' uses a structure like this:
        # "/hmi/knob1": {'addrs': [], 'idx': 0}
        hw = get_hardware()

        if "actuators" not in hw.keys():
            self.addressings = {}
            self._hw2uri_map = {}
            self._uri2hw_map = {}
            return

        self.addressings = dict((act["uri"], {'idx': 0, 'addrs': []}) for act in hw["actuators"])

        # Store all possible hardcoded values
        self._hw2uri_map = {}
        self._uri2hw_map = {}

        for i in range(0, 4):
            knob_hw  = (HARDWARE_TYPE_MOD, 0, ACTUATOR_TYPE_KNOB,       i)
            foot_hw  = (HARDWARE_TYPE_MOD, 0, ACTUATOR_TYPE_FOOTSWITCH, i)
            knob_uri = "/hmi/knob%i"       % (i+1)
            foot_uri = "/hmi/footswitch%i" % (i+1)

            self._hw2uri_map[knob_hw]  = knob_uri
            self._hw2uri_map[foot_hw]  = foot_uri
            self._uri2hw_map[knob_uri] = knob_hw
            self._uri2hw_map[foot_uri] = foot_hw

    # -----------------------------------------------------------------------------------------------------------------

    def _addressing_load(self, actuator_uri, callback, value=None, skipPresets=False):
        addressings       = self.addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_idx   = addressings['idx']

        try:
            addressing = addressings_addrs[addressings_idx]
        except IndexError:
            print("Failed to get addressing for", actuator_uri)
            callback(False)
            return

        actuator_hw = self._uri2hw_map[actuator_uri]
        plugin      = self.plugins[addressing['instance_id']]
        portsymbol  = addressing['port']

        if value is not None:
            curvalue = value
        elif portsymbol == ":bypass":
            curvalue = 1.0 if plugin['bypassed'] else 0.0
        elif portsymbol == ":presets":
            if skipPresets:
                print("NOTE: skipping re-address of presets")
                callback(True)
                return
            curvalue = plugin['mapPresets'].index(plugin['preset'])
        else:
            curvalue = plugin['ports'][portsymbol]

        self.hmi.control_add(addressing['instance_id'], portsymbol,
                             addressing['label'], addressing['type'], addressing['unit'],
                             curvalue, addressing['minimum'], addressing['maximum'], addressing['steps'],
                             actuator_hw[0], actuator_hw[1], actuator_hw[2], actuator_hw[3],
                             len(addressings_addrs), # num controllers
                             addressings_idx+1,      # index
                             addressing['options'], callback)

    def _address_next(self, actuator_hw, callback):
        actuator_uri = self._hw2uri_map[actuator_hw]

        addressings       = self.addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_idx   = addressings['idx']

        if len(addressings_addrs) > 0:
            addressings['idx'] = (addressings_idx + 1) % len(addressings_addrs)
            self._addressing_load(actuator_uri, callback)
        else:
            self.hmi.control_clean(actuator_hw[0], actuator_hw[1], actuator_hw[2], actuator_hw[3], callback)

    def _address_prev(self, actuator_hw, callback):
        actuator_uri = self._hw2uri_map[actuator_hw]

        addressings       = self.addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_idx   = addressings['idx']

        if len(addressings_addrs) > 0:
            addressings['idx'] = (addressings_idx - 1) % len(addressings_addrs)
            self._addressing_load(actuator_uri, callback)
        else:
            self.hmi.control_clean(actuator_hw[0], actuator_hw[1], actuator_hw[2], actuator_hw[3], callback)

    def _unaddress(self, pluginData, port):
        addressing = pluginData['addressings'].pop(port, None)
        if addressing is None:
            return None

        actuator_uri      = addressing['actuator_uri']
        addressings       = self.addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_idx   = addressings['idx']

        index = addressings_addrs.index(addressing)
        addressings_addrs.pop(index)

        # FIXME ?
        if addressings_idx >= index:
            addressings['idx'] -= 1
        #if index <= addressings_idx:
            #addressings['idx'] = addressings_idx - 1

        return actuator_uri

    @gen.coroutine
    def _load_addressings(self, bundlepath):
        datafile = os.path.join(bundlepath, "addressings.json")
        if not os.path.exists(datafile):
            return

        with open(datafile, 'r') as fh:
            data = fh.read()
        data = json.loads(data)

        used_actuators = []

        for actuator_uri in data:
            for addr in data[actuator_uri]:
                try:
                    instance_id = self.mapper.get_id_without_creating(addr['instance'])
                except KeyError:
                    continue

                plugin     = self.plugins[instance_id]
                portsymbol = addr['port']

                if portsymbol == ":bypass":
                    curvalue = 1.0 if plugin['bypassed'] else 0.0
                elif portsymbol == ":presets":
                    # this value is changed during addressing, don't bother setting it now
                    curvalue = 0
                else:
                    curvalue = plugin['ports'][portsymbol]

                self.address(addr["instance"], portsymbol, actuator_uri, addr["label"],
                             addr["minimum"], addr["maximum"], curvalue, addr["steps"], lambda r:None, True)

                if actuator_uri not in used_actuators:
                    used_actuators.append(actuator_uri)

        for actuator_uri in used_actuators:
            actuator_hw = self._uri2hw_map[actuator_uri]
            yield gen.Task(self._address_next, actuator_hw)

    # -----------------------------------------------------------------------------------------------------------------
    # HMI callbacks, called by HMI via serial

    def hmi_hardware_connected(self, hardware_type, hardware_id, callback):
        logging.info("hmi hardware connected")
        callback(True)

    def hmi_hardware_disconnected(self, hardware_type, hardware_id, callback):
        logging.info("hmi hardware disconnected")
        callback(True)

    def hmi_list_banks(self, callback):
        logging.info("hmi list banks")

        if len(self.allpedalboards) == 0:
            callback(True, "")
            return

        banks = "All 0"

        if len(self.banks) > 0:
            banks += " "
            banks += " ".join('"%s" %d' % (bank['title'], i+1) for i, bank in enumerate(self.banks))

        callback(True, banks)

    def hmi_list_bank_pedalboards(self, bank_id, callback):
        logging.info("hmi list bank pedalboards")

        if bank_id < 0 or bank_id > len(self.banks):
            print("ERROR: Trying to list pedalboards using out of bounds bank id %i" % (bank_id))
            callback(False, "")
            return

        if bank_id == 0:
            pedalboards = self.allpedalboards
        else:
            pedalboards = self.banks[bank_id-1]['pedalboards']

        numBytesFree = 1024-64
        pedalboardsData = None

        num = 0
        for pb in pedalboards:
            if num > 50:
                break

            title   = pb['title'].replace('"', '').upper()[:31]
            data    = '"%s" %i' % (title, num)
            dataLen = len(data)

            if numBytesFree-dataLen-2 < 0:
                print("ERROR: Controller out of memory when listing pedalboards (stopping at %i)" % num)
                break

            num += 1

            if pedalboardsData is None:
                pedalboardsData = ""
            else:
                pedalboardsData += " "

            numBytesFree -= dataLen+1
            pedalboardsData += data

        if pedalboardsData is None:
            pedalboardsData = ""

        callback(True, pedalboardsData)

    def hmi_load_bank_pedalboard(self, bank_id, pedalboard_id, callback):
        logging.info("hmi load bank pedalboard")

        if bank_id < 0 or bank_id > len(self.banks):
            print("ERROR: Trying to load pedalboard using out of bounds bank id %i" % (bank_id))
            callback(False)
            return

        try:
            pedalboard_id = int(pedalboard_id)
        except:
            print("ERROR: Trying to load pedalboard using invalid pedalboard_id '%s'" % (pedalboard_id))
            callback(False)
            return

        if self.next_hmi_pedalboard is not None:
            print("NOTE: Delaying loading of %i:%i" % (bank_id, pedalboard_id))
            self.next_hmi_pedalboard = (bank_id, pedalboard_id)
            callback(False)
            return

        if bank_id == 0:
            pedalboards = self.allpedalboards
            navigateFootswitches = False
            navigateChannel      = 15
        else:
            bank        = self.banks[bank_id-1]
            pedalboards = bank['pedalboards']
            navigateFootswitches = bank['navigateFootswitches']

            if "navigateChannel" in bank.keys() and not navigateFootswitches:
                navigateChannel = int(bank['navigateChannel'])-1
            else:
                navigateChannel = 15

        if pedalboard_id < 0 or pedalboard_id >= len(pedalboards):
            print("ERROR: Trying to load pedalboard using out of bounds pedalboard id %i" % (pedalboard_id))
            callback(False)
            return

        self.next_hmi_pedalboard = (bank_id, pedalboard_id)
        callback(True)

        bundlepath = pedalboards[pedalboard_id]['bundle']

        def loaded2_callback(ok):
            if self.next_hmi_pedalboard is None:
                print("ERROR: Delayed loading is in corrupted state")
                return
            if ok:
                print("NOTE: Delayed loading of %i:%i has started" % self.next_hmi_pedalboard)
            else:
                print("ERROR: Delayed loading of %i:%i failed!" % self.next_hmi_pedalboard)

        def loaded_callback(ok):
            print("NOTE: Loading of %i:%i finished" % (bank_id, pedalboard_id))

            # Check if there's a pending pedalboard to be loaded
            next_pedalboard = self.next_hmi_pedalboard
            self.next_hmi_pedalboard = None

            if next_pedalboard != (bank_id, pedalboard_id):
                self.hmi_load_bank_pedalboard(next_pedalboard[0], next_pedalboard[1], loaded2_callback)

        def load_callback(ok):
            self.bank_id = bank_id
            self.load(bundlepath)
            self.send_notmodified("midi_program_listen %d %d" % (int(not navigateFootswitches), navigateChannel),
                                  loaded_callback, datatype='boolean')

        def footswitch_callback(ok):
            self.setNavigateWithFootswitches(navigateFootswitches, load_callback)

        def hmi_clear_callback(ok):
            self.hmi.clear(footswitch_callback)

        self.reset(hmi_clear_callback)

    def hmi_parameter_get(self, instance_id, portsymbol, callback):
        logging.info("hmi parameter get")
        callback(self.plugins[instance_id]['ports'][portsymbol])

    def hmi_parameter_set(self, instance_id, portsymbol, value, callback):
        logging.info("hmi parameter set")
        instance = self.mapper.get_instance(instance_id)
        plugin   = self.plugins[instance_id]

        if portsymbol == ":bypass":
            bypassed = bool(value)
            plugin['bypassed'] = bypassed
            self.send_modified("bypass %d %d" % (instance_id, int(bypassed)), callback, datatype='boolean')
            self.msg_callback("param_set %s :bypass %f" % (instance, 1.0 if bypassed else 0.0))

            enabled_symbol = plugin['designations'][0]
            if enabled_symbol is None:
                return

            value = 0.0 if bypassed else 1.0
            plugin['ports'][enabled_symbol] = value
            self.msg_callback("param_set %s %s %f" % (instance, enabled_symbol, value))

        elif portsymbol == ":presets":
            value = int(value)
            if value < 0 or value >= len(plugin['mapPresets']):
                callback(False)
                return
            self.preset_load(instance, plugin['mapPresets'][value], callback)

        else:
            plugin['ports'][portsymbol] = value
            self.send_modified("param_set %d %s %f" % (instance_id, portsymbol, value), callback, datatype='boolean')
            self.msg_callback("param_set %s %s %f" % (instance, portsymbol, value))

    def hmi_parameter_addressing_next(self, hardware_type, hardware_id, actuator_type, actuator_id, callback):
        logging.info("hmi parameter addressing next")
        actuator_hw = (hardware_type, hardware_id, actuator_type, actuator_id)
        self._address_next(actuator_hw, callback)

    def hmi_parameter_addressing_prev(self, hardware_type, hardware_id, actuator_type, actuator_id, callback):
        logging.info("hmi parameter addressing prev")
        actuator_hw = (hardware_type, hardware_id, actuator_type, actuator_id)
        self._address_prev(actuator_hw, callback)

    def hmi_save_current_pedalboard(self, callback):
        logging.info("hmi save current pedalboard")
        titlesym = symbolify(self.pedalboard_name)[:16]
        self.save_state_mainfile(self.pedalboard_path, self.pedalboard_name, titlesym)
        callback(True)

    @gen.coroutine
    def hmi_reset_current_pedalboard(self, callback):
        logging.info("hmi reset current pedalboard")
        pb_values = get_pedalboard_plugin_values(self.pedalboard_path)
        used_actuators = []

        for p in pb_values:
            instance    = "/graph/%s" % p['instance']
            instance_id = self.mapper.get_id(instance)
            pluginData  = self.plugins[instance_id]

            bypassed = bool(p['bypassed'])
            pluginData['bypassed'] = bypassed

            self.send_notmodified("bypass %d %d" % (instance_id, 1 if bypassed else 0))

            addressing = pluginData['addressings'].get(":bypass", None)
            if addressing is not None and addressing['actuator_uri'] not in used_actuators:
                used_actuators.append(addressing['actuator_uri'])

            addressing = pluginData['addressings'].get(":presets", None)
            if addressing is not None and addressing['actuator_uri'] not in used_actuators:
                used_actuators.append(addressing['actuator_uri'])

            if p['preset']:
                preset = p['preset']
                pluginData['preset'] = preset
                self.send_notmodified("preset_load %d %s" % (instance_id, preset))

            for port in p['ports']:
                symbol = port['symbol']
                value  = port['value']

                pluginData['ports'][symbol] = value
                self.send_notmodified("param_set %d %s %f" % (instance_id, symbol, value))

                addressing = pluginData['addressings'].get(symbol, None)
                if addressing is not None and addressing['actuator_uri'] not in used_actuators:
                    used_actuators.append(addressing['actuator_uri'])

        self.pedalboard_modified = False
        callback(True)

        for actuator_uri in used_actuators:
            yield gen.Task(self._addressing_load, actuator_uri)

    def hmi_tuner(self, status, callback):
        if status == "on":
            self.hmi_tuner_on(callback)
        else:
            self.hmi_tuner_off(callback)

    def hmi_tuner_on(self, callback):
        logging.info("hmi tuner on")

        def monitor_added(ok):
            if not ok or not connect_jack_ports("system:capture_%d" % self.current_tuner_port,
                                                "effect_%d:%s" % (TUNER_INSTANCE, TUNER_INPUT_PORT)):
                self.send_notmodified("remove %d" % TUNER_INSTANCE)
                callback(False)
                return

            self.mute()
            callback(True)

        def tuner_added(ok):
            if not ok:
                callback(False)
                return
            self.send_notmodified("monitor_output %d %s" % (TUNER_INSTANCE, TUNER_MONITOR_PORT), monitor_added)

        self.send_notmodified("add %s %d" % (TUNER_URI, TUNER_INSTANCE), tuner_added)

    def hmi_tuner_off(self, callback):
        logging.info("hmi tuner off")

        def tuner_removed(ok):
            self.unmute()
            callback(True)

        self.send_notmodified("remove %d" % TUNER_INSTANCE, tuner_removed)

    def hmi_tuner_input(self, input_port, callback):
        logging.info("hmi tuner input")

        if 0 <= input_port > 2:
            callback(False)
            return

        disconnect_jack_ports("system:capture_%s" % self.current_tuner_port,
                              "effect_%d:%s" % (TUNER_INSTANCE, TUNER_INPUT_PORT))

        connect_jack_ports("system:capture_%s" % input_port,
                           "effect_%d:%s" % (TUNER_INSTANCE, TUNER_INPUT_PORT))

        self.current_tuner_port = input_port
        callback(True)

    @gen.coroutine
    def set_tuner_value(self, value):
        if value == 0.0:
            return

        freq, note, cents = find_freqnotecents(value)
        yield gen.Task(self.hmi.tuner, freq, note, cents)

    # -----------------------------------------------------------------------------------------------------------------
    # JACK stuff

    # Get list of Hardware MIDI devices
    # returns (devsInUse, devList, names)
    def get_midi_ports(self):
        out_ports = {}
        full_ports = {}

        # Current setup
        for port_symbol, port_alias, _ in self.midiports:
            port_aliases = port_alias.split(";",1)
            port_alias   = port_aliases[0]
            if len(port_aliases) != 1:
                out_ports[port_alias] = port_symbol
            full_ports[port_symbol] = port_alias

        # Extra MIDI Outs
        ports = get_jack_hardware_ports(False, True)
        for port in ports:
            if not port.startswith(("system:midi_", "nooice")):
                continue
            alias = get_jack_port_alias(port)
            if not alias:
                continue
            title = alias.split("-",5)[-1].replace("-"," ").replace(";",".")
            out_ports[title] = port

        # Extra MIDI Ins
        ports = get_jack_hardware_ports(False, False)
        for port in ports:
            if not port.startswith(("system:midi_", "nooice")):
                continue
            alias = get_jack_port_alias(port)
            if not alias:
                continue
            title = alias.split("-",5)[-1].replace("-"," ").replace(";",".")
            if title in out_ports.keys():
                port = "%s;%s" % (port, out_ports[title])
            full_ports[port] = title

        devsInUse = []
        devList = []
        names = {}
        midiportIds = tuple(i[0] for i in self.midiports)
        for port_id, port_alias in full_ports.items():
            devList.append(port_id)
            if port_id in midiportIds:
                devsInUse.append(port_id)
            names[port_id] = port_alias + (" (in+out)" if port_alias in out_ports else " (in)")

        devList.sort()
        return (devsInUse, devList, names)

    def get_port_name_alias(self, portname):
        alias = get_jack_port_alias(portname)

        if alias:
            return alias.split("-",5)[-1].replace("-"," ").replace(";",".")

        return portname.split(":",1)[-1].title()

    # Set the selected MIDI devices
    # Will remove or add new JACK ports (in mod-ui) as needed
    def set_midi_devices(self, newDevs):
        def add_port(name, title, isOutput):
            index = int(name[-1])

            if name.startswith("nooice"):
                index += 100

            self.msg_callback("add_hw_port /graph/%s midi %i %s %i" % (name.split(":",1)[-1], int(isOutput), title, index))

        def remove_port(name):
            removed_conns = []

            for ports in self.connections:
                jackports = (self._fix_host_connection_port(ports[0]), self._fix_host_connection_port(ports[1]))
                if name not in jackports:
                    continue
                disconnect_jack_ports(jackports[0], jackports[1])
                removed_conns.append(ports)

            for ports in removed_conns:
                self.connections.remove(ports)
                self.msg_callback("disconnect %s %s" % (ports[0], ports[1]))

            self.msg_callback("remove_hw_port /graph/%s" % (name.split(":",1)[-1]))

        midiportIds = tuple(i[0] for i in self.midiports)

        # remove
        for i in reversed(range(len(self.midiports))):
            port_symbol, port_alias, _ = self.midiports[i]
            if port_symbol in newDevs:
                continue

            if ";" in port_symbol:
                inp, outp = port_symbol.split(";",1)
                remove_port(inp)
                remove_port(outp)
            else:
                remove_port(port_symbol)

            self.midiports.pop(i)

        # add
        for port_symbol in newDevs:
            if port_symbol in midiportIds:
                continue

            if ";" in port_symbol:
                inp, outp = port_symbol.split(";",1)
                title_in  = self.get_port_name_alias(inp)
                title_out = self.get_port_name_alias(outp)
                title     = title_in + ";" + title_out
                add_port(inp, title_in, False)
                add_port(outp, title_out, True)
            else:
                title = self.get_port_name_alias(port_symbol)
                add_port(port_symbol, title, False)

            self.midiports.append([port_symbol, title, []])

    # -----------------------------------------------------------------------------------------------------------------
