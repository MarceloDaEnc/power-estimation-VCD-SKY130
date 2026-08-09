read_liberty sky130_fd_sc_hd__tt_025C_1v80.lib
read_verilog i2c_master_axil_nl_refactored.v
link_design i2c_master_axil
read_sdc i2c_master_axil.sdc
read_spef i2c_master_axil_nom_refactored.spef
read_vcd -scope i2c_netlist_wrapper/i2c_master_inst dump.vcd
report_power
exit
