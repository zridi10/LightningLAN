from lanshare.netutil import Interface, is_virtual_adapter, lan_addresses

ADAPTERS = [
    Interface("Software Loopback Interface 1", "127.0.0.1"),
    Interface("Realtek PCIe GbE Family Controller", "10.0.0.172"),
    Interface("Intel(R) Wi-Fi 6 AX201", "192.168.1.20"),
    Interface("Hyper-V Virtual Ethernet Adapter", "172.28.96.1"),
    Interface("vEthernet (WSL)", "172.20.192.1"),
    Interface("docker0", "172.17.0.1"),
    Interface("br-3f2a1b", "172.18.0.1"),
    Interface("VirtualBox Host-Only Ethernet Adapter", "192.168.56.1"),
    Interface("VMware Network Adapter VMnet8", "192.168.80.1"),
    Interface("Microsoft Wi-Fi Direct Virtual Adapter", "169.254.25.23"),
]


def test_skips_loopback_link_local_and_virtual_adapters():
    assert lan_addresses(interfaces=ADAPTERS, primary="10.0.0.172") == ["10.0.0.172", "192.168.1.20"]


def test_default_route_address_is_first():
    assert lan_addresses(interfaces=ADAPTERS, primary="192.168.1.20")[0] == "192.168.1.20"


def test_default_route_on_virtual_named_adapter_is_kept():
    # A Hyper-V external switch carries the real LAN on a "virtual" adapter.
    adapters = [Interface("Hyper-V Virtual Ethernet Adapter #3", "192.168.1.40"),
                Interface("Hyper-V Virtual Ethernet Adapter", "172.28.96.1")]
    assert lan_addresses(interfaces=adapters, primary="192.168.1.40") == ["192.168.1.40"]


def test_falls_back_to_loopback_without_a_lan():
    adapters = [Interface("lo", "127.0.0.1"), Interface("docker0", "172.17.0.1")]
    assert lan_addresses(interfaces=adapters, primary=None) == ["127.0.0.1"]


def test_explicit_interfaces_win():
    assert lan_addresses(["127.0.0.1"], interfaces=ADAPTERS, primary="10.0.0.172") == ["127.0.0.1"]


def test_virtual_adapter_names():
    assert is_virtual_adapter("docker0")
    assert is_virtual_adapter("vEthernet (Default Switch)")
    assert not is_virtual_adapter("Intel(R) Dual Band Wireless-AC 8260")
    assert not is_virtual_adapter("en0")
