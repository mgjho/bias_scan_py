import os
import queue
import sys
import time

import numpy as np
import pyqtgraph as pg
import pyvisa

os.environ["QT_API"] = "pyqt6"
from qtpy import QtCore, QtWidgets


class RequestHandler:
    """A wrapper around pyvisa that limits the rate of requests.

    Parameters
    ----------
    resource_name (str)
        The name of the resource.
    interval_ms (int)
        The interval in milliseconds between requests.
    **kwargs
        Additional keyword arguments to be passed to the resource.

    """

    def __init__(self, resource_name: str, interval_ms: int = 50, **kwargs):  # 50
        self.resource_name = resource_name
        self.interval_ms = interval_ms
        self._resource_kwargs = kwargs

    def open(self):
        """Open the pyvisa resource."""
        self.inst = pyvisa.ResourceManager().open_resource(
            self.resource_name, **self._resource_kwargs
        )
        self._last_update = time.perf_counter_ns()

    def wait_time(self):
        """Wait until the interval between requests has passed."""
        if self.interval_ms == 0:
            return
        while (time.perf_counter_ns() - self._last_update) <= self.interval_ms * 1e3:
            time.sleep(1e-4)

    def write(self, *args, **kwargs):
        self.wait_time()
        res = self.inst.write(*args, **kwargs)
        self._last_update = time.perf_counter_ns()
        return res

    def query(self, *args, **kwargs):
        self.wait_time()
        res = self.inst.query(*args, **kwargs)
        self._last_update = time.perf_counter_ns()
        return res

    def read(self, *args, **kwargs):
        """Read data from the resource.

        This is not very likely to be used. It may cause problems due to the wait time.
        Use `query` instead.
        """
        self.wait_time()
        res = self.inst.read(*args, **kwargs)
        self._last_update = time.perf_counter_ns()
        return res

    def close(self):
        self.inst.close()


class DAQThread(QtCore.QThread):
    sigReading = QtCore.Signal(float, float, float, bool)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._voltage_queue = queue.Queue()

    def run(self):
        agilent = RequestHandler("GPIB0::10::INSTR", interval_ms=1)
        agilent.open()
        agilent.write("*RST")
        agilent.write("OUTP ON")

        keithley = RequestHandler("GPIB0::14::INSTR", interval_ms=1)
        keithley.open()
        keithley.write("*RST")
        # keithley.write("CURR:NPLC 1")
        keithley.write("CURR:NPLC 6")
        keithley.write("CURR:RANG 2e-8")
        keithley.write("CONF:CURR")
        keithley.write("SYST:ZCH ON")
        keithley.write("SYST:ZCOR:ACQ")
        keithley.write("SYST:ZCOR ON")
        keithley.write("SYST:ZCH OFF")

        self._running = True

        while self._running:
            volt_changed = False
            if not self._voltage_queue.empty():
                volt_changed = True
                agilent.write(f"VOLT {self._voltage_queue.get()}")
                self._voltage_queue.task_done()
                # print(float(agilent.query("MEAS:CURR?")))
                # print(float(agilent.query("MEAS:CURR?")))

            volt = float(agilent.query("VOLT?"))

            amp, sec, _ = keithley.query("READ?").split(",")
            self.sigReading.emit(volt, float(amp[:-1]), float(sec), volt_changed)
            time.sleep(0.001)

        agilent.write("OUTP OFF")
        agilent.close()
        keithley.close()


class VoltageSetup(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()

        layout = QtWidgets.QFormLayout()
        self.setLayout(layout)

        self._start_spin = QtWidgets.QDoubleSpinBox()
        self._start_spin.setMinimum(0.0)
        self._start_spin.setMaximum(20.0)
        self._start_spin.setValue(0.0)
        layout.addRow("Start", self._start_spin)

        self._end_spin = QtWidgets.QDoubleSpinBox()
        self._end_spin.setMinimum(0.0)
        self._end_spin.setMaximum(50.0)
        self._end_spin.setValue(30.0)
        layout.addRow("End", self._end_spin)

        self._step_spin = QtWidgets.QDoubleSpinBox()
        self._step_spin.setDecimals(3)
        self._step_spin.setMinimum(0.001)
        self._step_spin.setMaximum(20.0)
        self._step_spin.setValue(0.05)
        layout.addRow("Step", self._step_spin)
        self.button = QtWidgets.QPushButton("Start Bias Scan")
        layout.addRow(self.button)

    @property
    def values(self):
        return np.arange(
            self._start_spin.value(), self._end_spin.value(), self._step_spin.value()
        )


class MainWindow(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Current Scan")

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        layout.addWidget(QtWidgets.QLabel("Hello, World!"))

        btn = QtWidgets.QPushButton("Start/Stop")
        btn.clicked.connect(self.toggle_daq)

        btn2 = QtWidgets.QPushButton("Clear Voltage Plot")
        btn2.clicked.connect(self.clear_vplot)

        btn3 = QtWidgets.QPushButton("Clear Current Plot")
        btn3.clicked.connect(self.clear_cplot)

        layout.addWidget(btn)
        layout.addWidget(btn3)

        self.p0 = pg.PlotWidget()
        self.p1 = pg.PlotWidget()
        layout.addWidget(self.p0)
        layout.addWidget(self.p1)

        self.voltage_setup = VoltageSetup()
        layout.addWidget(self.voltage_setup)
        layout.addWidget(btn2)
        self._amps = []
        self._secs = []

        self._p1_amps = []
        self._p1_volts = []

        self.voltage_setup.button.clicked.connect(self.on_voltage_setup)
        self.daq = DAQThread()
        self.daq.sigReading.connect(self.on_reading)

    def clear_vplot(self):
        self._p1_amps = []
        self._p1_volts = []
        self.p1.clear()

    def clear_cplot(self):
        self._amps = []
        self._secs = []
        self.p0.clear()

    @QtCore.Slot()
    def toggle_daq(self):
        if self.daq.isRunning():
            self.daq._running = False
            self.daq.wait()
        else:
            self.daq.start()

    def on_voltage_setup(self):
        for volt in self.voltage_setup.values:
            self.daq._voltage_queue.put(volt)

    @QtCore.Slot(float, float, float, bool)
    def on_reading(self, volt, amp, sec, volt_changed):
        self._amps.append(amp)
        self._secs.append(sec)
        self.p0.clear()
        self.p0.plot(np.array(self._secs) - self._secs[0], self._amps)

        if volt_changed:
            self._p1_amps.append(amp)
            self._p1_volts.append(volt)
            self.p1.clear()
            self.p1.plot(self._p1_volts, self._p1_amps)

    def closeEvent(self, event):
        if self.daq.isRunning():
            self.daq._running = False
            self.daq.wait()
        super().closeEvent(event)


if __name__ == "__main__":
    qapp = QtWidgets.QApplication(sys.argv)
    qapp.setStyle("Fusion")

    win = MainWindow()
    win.show()
    win.activateWindow()

    qapp.exec()
