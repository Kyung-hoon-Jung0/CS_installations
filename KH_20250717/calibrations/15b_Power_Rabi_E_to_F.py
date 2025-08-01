# %%
"""
POWER RABI WITH ERROR AMPLIFICATION
This sequence involves repeatedly executing the qubit pulse (such as x180, square_pi, or similar) 'N' times and
measuring the state of the resonator across different qubit pulse amplitudes and number of pulses.
By doing so, the effect of amplitude inaccuracies is amplified, enabling a more precise measurement of the pi pulse
amplitude. The results are then analyzed to determine the qubit pulse amplitude suitable for the selected duration.

Prerequisites:
    - Having found the resonance frequency of the resonator coupled to the qubit under study (resonator_spectroscopy).
    - Having calibrated the IQ mixer connected to the qubit drive line (external mixer or Octave port)
    - Having found the rough qubit frequency and pi pulse duration (rabi_chevron_duration or time_rabi).
    - Set the qubit frequency, desired pi pulse duration and rough pi pulse amplitude in the state.

Next steps before going to the next node:
    - Update the qubit pulse amplitude (pi_amp) in the state.
    - Save the current state by calling machine.save("quam")
"""


# %% {Imports}
from qualibrate import QualibrationNode, NodeParameters
from quam.components import pulses
from quam_libs.components import QuAM
from quam_libs.macros import qua_declaration
from quam_libs.lib.plot_utils import QubitGrid, grid_iter
from quam_libs.lib.save_utils import fetch_results_as_xarray
from quam_libs.lib.fit import fit_oscillation, oscillation
from qualang_tools.results import progress_counter, fetching_tool
from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.units import unit
from qm import SimulationConfig
from qm.qua import *
from typing import Literal, Optional, List
import matplotlib.pyplot as plt
import numpy as np


# %% {Node_parameters}
class Parameters(NodeParameters):
    qubits: Optional[List[str]] = ["q1"]
    num_averages: int = 200
    operation: str = "x180"
    min_amp_factor: float = 0.0
    max_amp_factor: float = 1.5
    amp_factor_step: float = 0.005
    simulate: bool = False
    timeout: int = 100


from pathlib import Path
script_name = Path(__file__).stem
node = QualibrationNode(name=script_name, parameters=Parameters())


# %% {Initialize_QuAM_and_QOP}
# Class containing tools to help handling units and conversions.
u = unit(coerce_to_integer=True)
# Instantiate the QuAM class from the state file
machine = QuAM.load()
# Generate the OPX and Octave configurations
config = machine.generate_config()
octave_config = machine.get_octave_config()
# Open Communication with the QOP
qmm = machine.connect()

# Get the relevant QuAM components
if node.parameters.qubits is None or node.parameters.qubits == "":
    qubits = machine.active_qubits
else:
    qubits = [machine.qubits[q] for q in node.parameters.qubits]
num_qubits = len(qubits)

for q in qubits:
    # Check if an optimized GEF frequency exists
    if not hasattr(q, "GEF_frequency_shift"):
        q.GEF_frequency_shift = 0


# %% {QUA_program}
operation = node.parameters.operation  # The qubit operation to play
n_avg = node.parameters.num_averages  # The number of averages

# Pulse amplitude sweep (as a pre-factor of the qubit pulse amplitude) - must be within [-2; 2)
amps = np.arange(
    node.parameters.min_amp_factor,
    node.parameters.max_amp_factor,
    node.parameters.amp_factor_step,
)

with program() as power_rabi:
    I, I_st, Q, Q_st, n, n_st = qua_declaration(num_qubits=num_qubits)
    a = declare(fixed)  # QUA variable for the qubit drive amplitude pre-factor
    npi = declare(int)  # QUA variable for the number of qubit pulses
    count = declare(int)  # QUA variable for counting the qubit pulses

    for i, qubit in enumerate(qubits):

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_(*from_array(a, amps)):
                update_frequency(
                    qubit.resonator.name,
                    qubit.resonator.intermediate_frequency + qubit.GEF_frequency_shift,
                )

                # Reset the qubit frequency
                update_frequency(qubit.xy.name, qubit.xy.intermediate_frequency)
                # Drive the qubit to the excited state
                qubit.xy.play(operation)
                # Update the qubit frequency to scan around the expected f_12
                update_frequency(
                    qubit.xy.name, qubit.xy.intermediate_frequency - qubit.anharmonicity
                )
                qubit.xy.play(operation, amplitude_scale=a)
                align()
                qubit.resonator.measure("readout", qua_vars=(I[i], Q[i]))
                save(I[i], I_st[i])
                save(Q[i], Q_st[i])
                qubit.resonator.wait(machine.thermalization_time * u.ns)

        align()

    with stream_processing():
        n_st.save("n")
        for i, qubit in enumerate(qubits):
            I_st[i].buffer(len(amps)).average().save(f"I{i + 1}")
            Q_st[i].buffer(len(amps)).average().save(f"Q{i + 1}")


# %% {Simulate_or_execute}
if node.parameters.simulate:
    # Simulates the QUA program for the specified duration
    simulation_config = SimulationConfig(duration=10_000)  # In clock cycles = 4ns
    job = qmm.simulate(config, power_rabi, simulation_config)
    job.get_simulated_samples().con1.plot()
    node.results = {"figure": plt.gcf()}
    node.machine = machine
    node.save()

else:
    qm = qmm.open_qm(config, close_other_machines=True)
    # with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
    job = qm.execute(power_rabi)

    # %% {Live_plot}
    results = fetching_tool(job, ["n"], mode="live")
    while results.is_processing():
        n = results.fetch_all()[0]
        progress_counter(n, n_avg, start_time=results.start_time)

    # %% {Data_fetching_and_dataset_creation}
    # Fetch the data from the OPX and convert it into a xarray with corresponding axes (from most inner to outer loop)
    ds = fetch_results_as_xarray(job.result_handles, qubits, {"amp": amps})
    # Derive the amplitude IQ_abs = sqrt(I**2 + Q**2)
    ds = ds.assign({"IQ_abs": np.sqrt(ds.I**2 + ds.Q**2)})
    # Add the qubit pulse absolute amplitude to the dataset
    ds = ds.assign_coords(
        {
            "abs_amp": (
                ["qubit", "amp"],
                np.array([q.xy.operations[operation].amplitude * amps for q in qubits]),
            )
        }
    )
    # Add the dataset to the node
    node.results = {"ds": ds}

    # %% {Data_analysis}
    # Fit the power Rabi oscillations
    fit = fit_oscillation(ds.IQ_abs, "amp")

    # Save fitting results
    fit_results = {}
    fit_evals = oscillation(
        ds.amp,
        fit.sel(fit_vals="a"),
        fit.sel(fit_vals="f"),
        fit.sel(fit_vals="phi"),
        fit.sel(fit_vals="offset"),
    )
    for q in qubits:
        fit_results[q.name] = {}
        f_fit = fit.loc[q.name].sel(fit_vals="f")
        phi_fit = fit.loc[q.name].sel(fit_vals="phi")
        # Ensure that phi is within [-pi/2, pi/2]
        phi_fit = phi_fit - np.pi * (phi_fit > np.pi / 2)
        # amplitude factor for getting an |e> -> |f> pi pulse
        factor = float(1.0 * (np.pi - phi_fit) / (2 * np.pi * f_fit))
        # Calibrated |e> -> |f> pi pulse absolute amplitude
        new_pi_amp = q.xy.operations[operation].amplitude * factor
        if np.abs(new_pi_amp) < 0.3:  # TODO: 1 for OPX1000 MW
            print(
                f"amplitude for E-F Pi pulse is modified by a factor of {factor:.2f} w.r.t the original pi pulse amplitude"
            )
            print(
                f"new amplitude is {1e3 * new_pi_amp:.2f} mV \n"
            )  # TODO: 1 for OPX1000 MW
            fit_results[q.name]["Pi_amplitude"] = new_pi_amp
        else:
            print(f"Fitted amplitude too high or negative, new amplitude is 300 mV \n")
            fit_results[q.name]["Pi_amplitude"] = 0.3  # TODO: 1 for OPX1000 MW
    node.results["fit_results"] = fit_results

    # %% {Plotting}
    grid_names = [f"{q.name}_0" for q in qubits]
    grid = QubitGrid(ds, grid_names)
    for ax, qubit in grid_iter(grid):
        (ds.assign_coords(amp_mV=ds.abs_amp * 1e3).loc[qubit].IQ_abs * 1e3).plot(
            ax=ax, x="amp_mV"
        )
        ax.plot(ds.abs_amp.loc[qubit] * 1e3, 1e3 * fit_evals.loc[qubit])
        ax.set_ylabel("Trans. amp. I [mV]")
        ax.set_xlabel("Amplitude [mV]")
        ax.set_title(qubit["qubit"])
    grid.fig.suptitle("Rabi : I vs. amplitude")
    plt.tight_layout()
    plt.show()
    node.results["figure"] = grid.fig

    # %% {Update_state}
    ef_operation_name = f"EF_{operation}"
    for q in qubits:
        if ef_operation_name not in q.xy.operations:
            # Create the |e> -> |f> operation
            q.xy.operations[ef_operation_name] = pulses.DragCosinePulse(
                amplitude=fit_results[q.name]["Pi_amplitude"],
                alpha=q.xy.operations[operation].alpha,
                anharmonicity=q.xy.operations[operation].anharmonicity,
                length=q.xy.operations[operation].length,
                axis_angle=0,  # TODO: to check that the rotation does not overwrite y-pulses
                digital_marker=q.xy.operations[operation].digital_marker,
            )
        else:
            with node.record_state_updates():
                # set the new amplitude for the EF operation
                q.xy.operations[ef_operation_name].amplitude = fit_results[q.name][
                    "Pi_amplitude"
                ]

    # %% {Save_results}
    node.outcomes = {q.name: "successful" for q in qubits}
    node.results["initial_parameters"] = node.parameters.model_dump()
    node.machine = machine
    node.save()
