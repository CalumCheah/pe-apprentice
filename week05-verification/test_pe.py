# test_pe.py
# A self-checking testbench for the PE, in Python. The same six components
# as the SystemVerilog version in pe_tb.sv, with the same TODOs.
#
# generator makes transactions (uvm_sequence)
# driver turns them into pin wiggles (uvm_driver)
# monitor watches the DUT, drives nothing
# model says what the answer should be
# scoreboard compares, counts, reports
# coverage says whether you tested enough
#
# Components talk through mailboxes, never by reaching into each other's
# variables. asyncio.Queue does not work inside cocotb, so the Mailbox
# below plays the mailbox from the SV version.

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Event, FallingEdge, ReadOnly, RisingEdge

DW = 16
ONE = 256 # 1.0 in Q8.8
MINV = -(1 << (DW - 1))
MAXV = (1 << (DW - 1)) - 1


def s16(x):
    x &= 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


# Given: Q8.8 arithmetic that matches rtl/fxp.sv exactly. The multiply
# truncates the low QF bits; the add saturates. Read them anyway.
def q_mul(a, b):
    return s16((a * b) >> 8)


def q_add(a, b):
    s = a + b
    if s > MAXV:
        return MAXV
    if s < MINV:
        return MINV
    return s


# Given. put() adds, get() waits until something is there.
class Mailbox:
    def __init__(self):
        self._items = []
        self._event = Event()

    def put(self, x):
        self._items.append(x)
        self._event.set()

    async def get(self):
        while not self._items:
            self._event.clear()
            await self._event.wait()
        return self._items.pop(0)


# 1. THE TRANSACTION. One operation on the PE: a weight, an activation, a
# partial sum, and the four control bits. It says WHAT should happen, not
# how to make it happen on wires.
class Txn:
    def __init__(self, weight, activation, psum_in,
        accept_w, switch, valid, enabled):
        self.weight = weight
        self.activation = activation
        self.psum_in = psum_in
        self.accept_w = accept_w
        self.switch = switch
        self.valid = valid
        self.enabled = enabled

    # Given. Used in scoreboard failure messages so you can see what was
    # being driven when something went wrong.
    def show(self):
        return (f"w={self.weight:6d} a={self.activation:6d} p={self.psum_in:6d} | "
        f"acc={self.accept_w} sw={self.switch} vld={self.valid} en={self.enabled}")


# 2. THE GENERATOR. Constrained random: you describe the space of legal
# inputs and how often you want each kind, and the simulator picks the
# actual values. rng.choices([1, 0], weights=[9, 1]) is dist { 1 := 9, 0 := 1 }.
def gen_txn(rng):
    return Txn(
        weight=rng.randint(-8 * ONE, 8 * ONE), # +-8.0, small enough not to saturate
        activation=rng.randint(-8 * ONE, 8 * ONE),
        psum_in=rng.randint(-8 * ONE, 8 * ONE),
        # TODO(week5): the four control bits. weights=[1, 1] (uniform) is a
        # placeholder, not an answer. Your week 4 stimulus recipe should
        # already have an opinion on each:
        # valid how often is the PE actually streaming?
        # enabled low rarely enough that the PE runs, often enough to matter
        # accept_w weights have to be loaded often enough to matter
        # switch every cycle is not realistic, never is not a test
        # Every weight you write needs a reason you can say out loud.
        accept_w=rng.choices([1, 0], weights=[25, 75])[0],
        switch=rng.choices([1, 0], weights=[20, 80])[0],
        valid=rng.choices([1, 0], weights=[80, 20])[0],
        enabled=rng.choices([1, 0], weights=[90, 10])[0],
    )


# 3. THE REFERENCE MODEL. What the PE SHOULD output, computed in software.
# Write step() from your week 2 spec, not by reading your own RTL.
class Model:
    def __init__(self):
        self.weight_bg = 0
        self.weight_fg = 0
        self.out = 0

    def reset(self):
        self.weight_bg = 0
        self.weight_fg = 0
        self.out = 0

    def step(self, t):
        # TODO(week5): advance the model one cycle and return what
        # pe_psum_out should be after this transaction is applied.
        # 1. Which weight does the multiplier read THIS cycle? (week 2 q5a)
        # 2. If accept_w is high, what changes, and does it affect this
        # cycle's answer or the next one?
        # 3. If enabled is low, what happens to the output, and to the two
        # stored weights? (self.out is here for a reason.)
        # 4. If valid is low, what is pe_psum_out?
        # 5. Otherwise: q_add(q_mul(activation, foreground weight), psum_in).
        # Software runs top to bottom; hardware does everything at once on
        # the clock edge. Compute this cycle's answer from the OLD state,
        # then update state. If you update weight_bg before you use it, you
        # have modelled a PE that does not exist.
        # A switch selects the old background for this cycle's MAC.
        active_weight = self.weight_bg if t.switch else self.weight_fg
        if t.enabled and t.valid:
            out = q_add(q_mul(t.activation, active_weight), t.psum_in)
        else:
            out = 0

        # Weight updates run even when the streaming outputs are disabled.
        # Accept happens last so a simultaneous switch uses the old background.
        self.weight_fg = active_weight
        if t.accept_w:
            self.weight_bg = t.weight

        self.out = out
        return self.out


# 4. THE DRIVER. The only component that drives anything. It works on the
# falling edge: the DUT samples on the rising edge, so the values are
# stable well before it looks at them.
async def driver(dut, gen2drv, drv2scb):
    while True:
        t = await gen2drv.get()
        await FallingEdge(dut.clk)
        dut.pe_weight_in.value = t.weight
        dut.pe_input_in.value = t.activation
        # TODO(week5): drive the five remaining inputs from t (pe_psum_in,
        # pe_accept_w_in, pe_switch_in, pe_valid_in, pe_enabled), then
        # drv2scb.put(t) so the scoreboard knows what was sent.
        dut.pe_psum_in.value = t.psum_in
        dut.pe_accept_w_in.value = t.accept_w
        dut.pe_switch_in.value = t.switch
        dut.pe_valid_in.value = t.valid
        dut.pe_enabled.value = t.enabled

        drv2scb.put(t)


# 5. THE MONITOR. Watches pe_psum_out and reports what it saw. It drives
# nothing. Given, because the timing offset is fiddly and is not the
# lesson: the DUT registers its output, so what is on pe_psum_out one
# falling edge after a transaction was driven is the response to it.
async def monitor(dut, mon2scb):
    await FallingEdge(dut.clk)
    while True:
        await FallingEdge(dut.clk)
        mon2scb.put(dut.pe_psum_out.value.to_signed())


# 6. THE SCOREBOARD. Pairs what was sent with what came back, asks the
# model what should have come back, and counts.
async def scoreboard(dut, drv2scb, mon2scb, model, stats, cov):
    while True:
        t = await drv2scb.get() # what we sent
        observed = await mon2scb.get() # what came back
        # TODO(week5): record this transaction in cov["x_switch_accept"],
        # ask the model for `expected`, compare, and count. On a mismatch
        # print the check number, t.show(), expected and observed, and use
        # dut._log.error(...) so the line stands out. Increment
        # stats["checked"] every time and stats["errors"] only on failure.
        # Leave the two lines below last.

        cov["x_switch_accept"].add((t.accept_w, t.switch))

        stats["checked"] += 1
        expected = model.step(t)
        if (expected != observed):
            stats["errors"] += 1
            dut._log.error(f"MISMATCH #{stats['checked']}: {t.show()} expected={expected} observed={observed}")

        if stats["checked"] >= stats["n"]:
            stats["done"].set()


# Given. If the run hangs, this fails it with a useful message instead.
async def watchdog(dut):
    for _ in range(10000):
        await FallingEdge(dut.clk)
    assert False, "timeout: a component is stuck - did you fill every TODO? Is a mailbox empty?"


@cocotb.test()
async def pe_weight_control_regressions(dut):
    # Each row is (weight, activation, psum, accept, switch, valid, enabled),
    # followed by a hand-calculated expected psum. Check the model as well
    # as all five DUT outputs so matching mistakes cannot hide a failure.
    scenarios = {
        "switch affects the same MAC": [
            ((3*ONE, 0, 0, 1, 0, 0, 1), 0),
            ((0, 2*ONE, ONE, 0, 1, 1, 1), 7*ONE),
            ((0, 2*ONE, ONE, 0, 0, 1, 1), 7*ONE),
        ],
        "load while disabled": [
            ((5*ONE, 0, 0, 1, 0, 0, 0), 0),
            ((0, 0, 0, 0, 1, 0, 1), 0),
            ((0, 2*ONE, ONE, 0, 0, 1, 1), 11*ONE),
        ],
        "switch while disabled": [
            ((3*ONE, 0, 0, 1, 0, 0, 1), 0),
            ((0, 0, 0, 0, 1, 0, 1), 0),
            ((5*ONE, 0, 0, 1, 0, 0, 1), 0),
            ((0, 2*ONE, ONE, 0, 1, 1, 0), 0),
            ((0, 2*ONE, ONE, 0, 0, 1, 1), 11*ONE),
        ],
        "accept and switch use the old background": [
            ((3*ONE, 0, 0, 1, 0, 0, 1), 0),
            ((5*ONE, 2*ONE, ONE, 1, 1, 1, 1), 7*ONE),
            ((0, 2*ONE, ONE, 0, 0, 1, 1), 7*ONE),
            ((0, 2*ONE, ONE, 0, 1, 1, 1), 11*ONE),
            ((3*ONE, 2*ONE, ONE, 1, 1, 1, 1), 11*ONE),
            ((0, 2*ONE, ONE, 0, 1, 1, 1), 7*ONE),
        ],
    }
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    failures = []
    for scenario, rows in scenarios.items():
        await FallingEdge(dut.clk)
        dut.rst.value = 1
        dut.pe_psum_in.value = 0
        dut.pe_weight_in.value = 0
        dut.pe_input_in.value = 0
        dut.pe_accept_w_in.value = 0
        dut.pe_valid_in.value = 0
        dut.pe_switch_in.value = 0
        dut.pe_enabled.value = 1
        for _ in range(2):
            await FallingEdge(dut.clk)
        dut.rst.value = 0
        model = Model()

        for fields, expected_psum in rows:
            t = Txn(*fields)
            await FallingEdge(dut.clk)
            dut.pe_weight_in.value = t.weight
            dut.pe_input_in.value = t.activation
            dut.pe_psum_in.value = t.psum_in
            dut.pe_accept_w_in.value = t.accept_w
            dut.pe_switch_in.value = t.switch
            dut.pe_valid_in.value = t.valid
            dut.pe_enabled.value = t.enabled
            await RisingEdge(dut.clk)
            await ReadOnly()

            observed = (
                dut.pe_psum_out.value.to_signed(),
                dut.pe_weight_out.value.to_signed(),
                dut.pe_input_out.value.to_signed(),
                int(dut.pe_valid_out.value),
                int(dut.pe_switch_out.value),
            )
            expected = (
                expected_psum,
                t.weight if t.enabled and t.accept_w else 0,
                t.activation if t.enabled and t.valid else 0,
                int(t.enabled and t.valid),
                int(t.enabled and t.switch),
            )
            model_psum = model.step(t)
            if observed != expected or model_psum != expected_psum:
                failures.append(
                    f"{scenario}: {t.show()} expected={expected}, "
                    f"observed={observed}, model_psum={model_psum}"
                )

    assert not failures, "\n".join(failures)


@cocotb.test()
async def pe_constrained_random(dut):
    N = 500
    rng = random.Random(1) # a fixed seed makes a failing run reproducible

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())

    dut.rst.value = 1
    dut.pe_psum_in.value = 0
    dut.pe_weight_in.value = 0
    dut.pe_input_in.value = 0
    dut.pe_accept_w_in.value = 0
    dut.pe_valid_in.value = 0
    dut.pe_switch_in.value = 0
    dut.pe_enabled.value = 1
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst.value = 0

    model = Model()
    gen2drv = Mailbox()
    drv2scb = Mailbox()
    mon2scb = Mailbox()
    stats = {"checked": 0, "errors": 0, "n": N, "done": Event()}
    cov = {"x_switch_accept": set()}

    # Fill the generator's mailbox first so the driver never stalls, which
    # keeps the driver and monitor streams in lockstep.
    for _ in range(N):
        gen2drv.put(gen_txn(rng))

    cocotb.start_soon(watchdog(dut))
    cocotb.start_soon(driver(dut, gen2drv, drv2scb))
    cocotb.start_soon(monitor(dut, mon2scb))
    cocotb.start_soon(scoreboard(dut, drv2scb, mon2scb, model, stats, cov))

    await stats["done"].wait()

    print("")
    print("=======================================================")
    print(f" checked : {stats['checked']}")
    print(f" errors : {stats['errors']}")
    print(f" x_switch_accept coverage: {100.0 * len(cov['x_switch_accept']) / 4:.1f}%")
    print("=======================================================")
    print(" PASS" if stats["errors"] == 0 else " FAIL")
    assert stats["errors"] == 0
