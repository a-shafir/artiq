#!/usr/bin/env python3

import argparse
import os
from collections import namedtuple

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from misoc.cores import gpio
from misoc.integration.soc_sdram import soc_sdram_args, soc_sdram_argdict
from misoc.integration.builder import builder_args, builder_argdict
from misoc.interconnect import stream
from misoc.interconnect.csr import *
from misoc.targets.sayma_amc import MiniSoC

from jesd204b.common import (JESD204BTransportSettings,
                             JESD204BPhysicalSettings,
                             JESD204BSettings)
from jesd204b.phy.gth import GTHQuadPLL as JESD204BGTHQuadPLL
from jesd204b.phy import JESD204BPhyTX
from jesd204b.core import JESD204BCoreTX
from jesd204b.core import JESD204BCoreTXControl

from artiq.gateware.amp import AMPSoC, build_artiq_soc
from artiq.gateware import serwb
from artiq.gateware import remote_csr
from artiq.gateware import rtio
from artiq.gateware.rtio.phy import ttl_simple, sawg
from artiq import __version__ as artiq_version


PhyPads = namedtuple("PhyPads", "txp txn")
to_jesd = ClockDomainsRenamer("jesd")


class AD9154JESD(Module, AutoCSR):
    def __init__(self, platform):
        self.jreset = CSRStorage(reset=1)

        ps = JESD204BPhysicalSettings(l=8, m=4, n=16, np=16)
        ts = JESD204BTransportSettings(f=2, s=2, k=16, cs=0)
        settings = JESD204BSettings(ps, ts, did=0x5a, bid=0x5)
        linerate = 10e9
        refclk_freq = 250e6
        fabric_freq = 125*1000*1000

        self.refclk = Signal()
        refclk2 = Signal()
        self.clock_domains.cd_jesd = ClockDomain()
        refclk_pads = platform.request("dac_refclk")

        self.specials += [
            Instance("IBUFDS_GTE3", i_CEB=0, p_REFCLK_HROW_CK_SEL=0b00,
                     i_I=refclk_pads.p, i_IB=refclk_pads.n,
                     o_O=self.refclk, o_ODIV2=refclk2),
            Instance("BUFG_GT", i_I=refclk2, o_O=self.cd_jesd.clk),
            AsyncResetSynchronizer(self.cd_jesd, self.jreset.storage),
        ]
        self.cd_jesd.clk.attr.add("keep")
        platform.add_period_constraint(self.cd_jesd.clk, 1e9/refclk_freq)

        self.phys = []
        for dac in range(2):
            jesd_pads = platform.request("dac_jesd", dac)
            phys = []
            self.phys.append(phys)
            for i in range(len(jesd_pads.txp)):
                if i % 4 == 0:
                    qpll = JESD204BGTHQuadPLL(
                            self.refclk, refclk_freq, linerate)
                    self.submodules += qpll
                    print(qpll)  # FIXME
                phy = JESD204BPhyTX(
                        qpll, PhyPads(jesd_pads.txp[i], jesd_pads.txn[i]),
                        fabric_freq, transceiver="gth")
                phy.transmitter.cd_tx.clk.attr.add("keep")
                platform.add_period_constraint(phy.transmitter.cd_tx.clk,
                        40*1e9/linerate)
                platform.add_false_path_constraints(
                #    self.crg.cd_sys.clk,  FIXME?
                    self.cd_jesd.clk,
                    phy.transmitter.cd_tx.clk)
                phys.append(phy)

            core = to_jesd(JESD204BCoreTX(
                phys, settings, converter_data_width=64))
            setattr(self.submodules, "core{}".format(dac), core)
            control = to_jesd(JESD204BCoreTXControl(core))
            setattr(self.submodules, "control{}".format(dac), control)
            core.register_jsync(platform.request("dac_sync", dac))

        # self.comb += platform.request("user_led", 3).eq(self.core0.jsync)

        # blinking leds for transceiver reset status
        #for i in range(4):
        #    counter = Signal(max=fabric_freq)
        #    self.comb += platform.request("user_led", 4 + i).eq(counter[-1])
        #    sync = getattr(self.sync, "phy{}_tx".format(i))
        #    sync += [
        #        counter.eq(counter - 1),
        #        If(counter == 0,
        #            counter.eq(fabric_freq - 1)
        #        )
        #    ]


class AD9154(Module, AutoCSR):
    def __init__(self, platform):
        self.submodules.jesd = AD9154JESD(platform)

        self.sawgs = [sawg.Channel(width=16, parallelism=8) for i in range(8)]
        self.submodules += self.sawgs

        # for i in range(len(self.sawgs)):
        #    self.sawgs[i].connect_y(self.sawgs[i ^ 1])

        for conv, ch in zip(
                self.jesd.core0.sink.flatten() +
                self.jesd.core1.sink.flatten(),
                self.sawgs):
            self.sync.jesd += conv.eq(Cat(ch.o))


class SaymaAMCStandalone(MiniSoC, AMPSoC):
    mem_map = {
        "cri_con":       0x10000000,
        "rtio":          0x11000000,
        "rtio_dma":      0x12000000,
        "serwb":         0x13000000,
        "ad9154":        0x14000000,
        "mailbox":       0x70000000
    }
    mem_map.update(MiniSoC.mem_map)

    def __init__(self, cpu_type="or1k", with_sawg=False, **kwargs):
        MiniSoC.__init__(self,
                         cpu_type=cpu_type,
                         sdram_controller_type="minicon",
                         l2_size=128*1024,
                         ident=artiq_version,
                         ethmac_nrxslots=4,
                         ethmac_ntxslots=4,
                         **kwargs)
        AMPSoC.__init__(self)
        platform = self.platform
        platform.toolchain.bitstream_commands.append(
            "set_property BITSTREAM.GENERAL.COMPRESS True [current_design]")

        self.submodules.leds = gpio.GPIOOut(Cat(
            platform.request("user_led", 0),
            platform.request("user_led", 1)))
        self.csr_devices.append("leds")

        # forward RTM UART to second FTDI UART channel
        serial_1 = platform.request("serial", 1)
        serial_rtm = platform.request("serial_rtm")
        self.comb += [
            serial_1.tx.eq(serial_rtm.rx),
            serial_rtm.tx.eq(serial_1.rx)
        ]

        # AMC/RTM serwb
        serwb_pll = serwb.phy.SERWBPLL(125e6, 1.25e9, vco_div=2)
        self.comb += serwb_pll.refclk.eq(self.crg.cd_sys.clk)
        self.submodules += serwb_pll

        serwb_pads = platform.request("amc_rtm_serwb")
        serwb_phy = serwb.phy.SERWBPHY(platform.device, serwb_pll, serwb_pads, mode="master")
        self.submodules.serwb_phy = serwb_phy
        self.csr_devices.append("serwb_phy")

        serwb_phy.serdes.cd_serwb_serdes.clk.attr.add("keep")
        serwb_phy.serdes.cd_serwb_serdes_20x.clk.attr.add("keep")
        serwb_phy.serdes.cd_serwb_serdes_5x.clk.attr.add("keep")
        platform.add_period_constraint(serwb_phy.serdes.cd_serwb_serdes.clk, 32.0),
        platform.add_period_constraint(serwb_phy.serdes.cd_serwb_serdes_20x.clk, 1.6),
        platform.add_period_constraint(serwb_phy.serdes.cd_serwb_serdes_5x.clk, 6.4)
        platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            serwb_phy.serdes.cd_serwb_serdes.clk,
            serwb_phy.serdes.cd_serwb_serdes_5x.clk)

        serwb_core = serwb.core.SERWBCore(serwb_phy, int(self.clk_freq), mode="slave")
        self.submodules += serwb_core
        self.add_wb_slave(self.mem_map["serwb"], 8192, serwb_core.etherbone.wishbone.bus)

        # RTIO
        rtio_channels = []
        for i in (2, 3):
            phy = ttl_simple.Output(platform.request("user_led", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy))

        for i in (0, 1):
            sma_io = platform.request("sma_io", i)
            self.comb += sma_io.direction.eq(1)
            phy = ttl_simple.Output(sma_io.level)
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy))

        if with_sawg:
            self.submodules.ad9154_0 = AD9154(platform)
            self.csr_devices.append("ad9154_0")
            self.config["HAS_AD9154"] = None
            self.add_csr_group("ad9154", ["ad9154_0"])
            self.config["RTIO_FIRST_SAWG_CHANNEL"] = len(rtio_channels)
            rtio_channels.extend(rtio.Channel.from_phy(phy)
                                for sawg in self.ad9154_0.sawgs
                                for phy in sawg.phys)

        self.config["HAS_RTIO_LOG"] = None
        self.config["RTIO_LOG_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.LogChannel())

        self.clock_domains.cd_rtio = ClockDomain()
        self.comb += [
            self.cd_rtio.clk.eq(ClockSignal()),
            self.cd_rtio.rst.eq(ResetSignal())
        ]
        self.submodules.rtio_core = rtio.Core(rtio_channels)
        self.csr_devices.append("rtio_core")
        self.submodules.rtio = rtio.KernelInitiator()
        self.submodules.rtio_dma = ClockDomainsRenamer("sys_kernel")(
            rtio.DMA(self.get_native_sdram_if()))
        self.register_kernel_cpu_csrdevice("rtio")
        self.register_kernel_cpu_csrdevice("rtio_dma")
        self.submodules.cri_con = rtio.CRIInterconnectShared(
            [self.rtio.cri, self.rtio_dma.cri],
            [self.rtio_core.cri])
        self.register_kernel_cpu_csrdevice("cri_con")
        self.submodules.rtio_moninj = rtio.MonInj(rtio_channels)
        self.csr_devices.append("rtio_moninj")

        self.submodules.rtio_analyzer = rtio.Analyzer(self.rtio_core.cri,
                                                      self.get_native_sdram_if())
        self.csr_devices.append("rtio_analyzer")


def main():
    parser = argparse.ArgumentParser(
        description="ARTIQ device binary builder / Sayma AMC stand-alone")
    builder_args(parser)
    soc_sdram_args(parser)
    parser.add_argument("--rtm-csr-csv",
        default=os.path.join("artiq_sayma_rtm", "sayma_rtm_csr.csv"),
        help="CSV file listing remote CSRs on RTM (default: %(default)s)")
    parser.add_argument("--with-sawg",
        default=False, action="store_true",
        help="add JESD204B and SAWG channels (default: %(default)s)")
    args = parser.parse_args()

    soc = SaymaAMCStandalone(with_sawg=args.with_sawg,
            **soc_sdram_argdict(args))

    remote_csr_regions = remote_csr.get_remote_csr_regions(
        soc.mem_map["serwb"] | soc.shadow_base,
        args.rtm_csr_csv)
    for name, origin, busword, csrs in remote_csr_regions:
        soc.add_csr_region(name, origin, busword, csrs)
    # Configuration for RTM peripherals. Keep in sync with sayma_rtm.py!
    soc.config["HAS_HMC830_7043"] = None
    soc.config["CONVERTER_SPI_HMC830_CS"] = 0
    soc.config["CONVERTER_SPI_HMC7043_CS"] = 1
    soc.config["CONVERTER_SPI_FIRST_AD9154_CS"] = 2

    build_artiq_soc(soc, builder_argdict(args))


if __name__ == "__main__":
    main()
