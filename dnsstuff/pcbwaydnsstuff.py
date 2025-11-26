from typing import List, Set

import dns.resolver
import dns.rdtypes.ANY.SPF
import dns.rdtypes.ANY.TXT

import socket
import random
import string
import sys
import os

from pyroute2.ipset import IPSet, PortEntry, PortRange


def ipsettest() -> None:
    ipset = IPSet()
    ipset.swap("oldset", "newset")

    ipset.create("foo", stype="hash:ip")
    ipset.add("foo", "198.51.100.1", etype="ip")
    ipset.add("foo", "198.51.100.2", etype="ip")
    print(ipset.test("foo", "198.51.100.1"))  # True
    print(ipset.test("foo", "198.51.100.10"))  # False
    msg_list = ipset.list("foo")
    for msg in msg_list:
        for attr_data in msg.get_attr("IPSET_ATTR_ADT").get_attrs("IPSET_ATTR_DATA"):
            for attr_ip_from in attr_data.get_attrs("IPSET_ATTR_IP_FROM"):
                for ipv4 in attr_ip_from.get_attrs("IPSET_ATTR_IPADDR_IPV4"):
                    print("- " + ipv4)
    ipset.destroy("foo")
    ipset.close()

    ipset = IPSet()
    ipset.create("bar", stype="bitmap:port", bitmap_ports_range=(1000, 2000))
    ipset.add("bar", 1001, etype="port")
    ipset.add("bar", PortRange(1500, 2000), etype="port")
    print(ipset.test("bar", 1600, etype="port"))  # True
    print(ipset.test("bar", 2600, etype="port"))  # False
    ipset.destroy("bar")
    ipset.close()

    ipset = IPSet()
    protocol_tcp = socket.getprotobyname("tcp")
    ipset.create("foobar", stype="hash:net,port")
    port_entry_http = PortEntry(80, protocol=protocol_tcp)
    ipset.add("foobar", ("198.51.100.0/24", port_entry_http), etype="net,port")
    print(ipset.test("foobar", ("198.51.100.1", port_entry_http), etype="ip,port"))  # True
    port_entry_https = PortEntry(443, protocol=protocol_tcp)
    print(ipset.test("foobar", ("198.51.100.1", port_entry_https), etype="ip,port"))  # False
    ipset.destroy("foobar")
    ipset.close()


def get_spf_records(domain: str) -> List[str]:
    """
    Retrieves SPF records for a specific domain.
    SPF records are typically stored in TXT records.

    Args:
        domain: The domain name for which SPF records should be retrieved

    Returns:
        List of found SPF record strings
    """
    spf_records = []

    try:
        # Query TXT records, as SPF records are stored there
        answers = dns.resolver.resolve(domain, "TXT")
        txt_rdata: dns.rdtypes.ANY.TXT.TXT

        spf_found = False
        print(f"SPF records for {domain}:")

        for txt_rdata in answers:
            # TXT records can consist of multiple strings
            txt_content = "".join([s.decode("utf-8") if isinstance(s, bytes) else s for s in txt_rdata.strings])

            # Check if it's an SPF record (starts with "v=spf1")
            if txt_content.startswith("v=spf1"):
                spf_found = True
                spf_records.append(txt_content)

                # Create an SPF object for correct typing
                spf_rdata: dns.rdtypes.ANY.SPF.SPF = dns.rdtypes.ANY.SPF.SPF(
                    txt_rdata.rdclass, txt_rdata.rdtype, txt_rdata.strings
                )

                print(f"{type(spf_rdata)=}")
                print(f"SPF Record: {txt_content}")
                print(f"SPF Record (to_text): {spf_rdata.to_text()}")
                print()

        if not spf_found:
            print(f"No SPF records found in TXT records for {domain}")

    except dns.resolver.NoAnswer:
        print(f"No TXT records found for {domain}")
    except dns.resolver.NXDOMAIN:
        print(f"Domain {domain} does not exist")
    except Exception as e:
        print(f"Error retrieving SPF records: {e}")

    return spf_records


def resolve_spf_to_ipv4(domain: str, visited_domains: Set | None = None) -> List[str]:
    """
    Resolves SPF records recursively and collects all IPv4 addresses.

    Args:
        domain: The domain name for which SPF records should be resolved
        visited_domains: Set of already visited domains (prevents infinite loops)

    Returns:
        List of all found IPv4 addresses/networks
    """
    if visited_domains is None:
        visited_domains = set()

    # Avoid infinite loops with circular includes
    if domain in visited_domains:
        print(f"Domain {domain} already visited, skipping...")
        return []

    visited_domains.add(domain)
    ipv4_addresses: List[str] = []

    # Get SPF records for the domain
    spf_records = get_spf_records(domain)

    if not spf_records:
        return ipv4_addresses

    # Process each SPF record
    for spf_record in spf_records:
        # Split the SPF record into individual mechanisms
        mechanisms = spf_record.split()

        for mechanism in mechanisms:
            # Extract direct IPv4 addresses
            if mechanism.startswith("ip4:"):
                ipv4 = mechanism[4:]  # Remove 'ip4:' prefix
                ipv4_addresses.append(ipv4)
                print(f"  → Found IPv4: {ipv4}")

            # Process include directives recursively
            elif mechanism.startswith("include:"):
                include_domain = mechanism[8:]  # Remove 'include:' prefix
                print(f"\n  → Processing include: {include_domain}")

                # Recursive call for the include domain
                included_ipv4s: List[str] = resolve_spf_to_ipv4(include_domain, visited_domains)
                ipv4_addresses.extend(included_ipv4s)

            # Process MX mechanisms
            elif mechanism.startswith("mx:") or mechanism == "mx":
                # Determine the domain for the MX query
                if mechanism == "mx":
                    mx_domain = domain  # Use the current domain
                else:
                    mx_domain = mechanism[3:]  # Remove 'mx:' prefix

                print(f"\n  → Processing MX: {mx_domain}")

                try:
                    # Get MX records
                    mx_answers = dns.resolver.resolve(mx_domain, "MX")

                    for mx_rdata in mx_answers:
                        mx_host = str(mx_rdata.exchange).rstrip(".")
                        print(f"    → MX host found: {mx_host}")

                        try:
                            # Resolve A records (IPv4) for the MX host
                            # dns.resolver.resolve() follows CNAMEs automatically,
                            # but only returns the final A records
                            a_answers = dns.resolver.resolve(mx_host, "A")

                            for a_rdata in a_answers:
                                ipv4 = str(a_rdata)
                                ipv4_addresses.append(ipv4)
                                print(f"      → Found IPv4 (MX): {ipv4}")

                            # Check if CNAMEs were involved (for debugging purposes)
                            if hasattr(a_answers, "canonical_name") and a_answers.canonical_name != dns.name.from_text(
                                mx_host
                            ):
                                print(f"      → (via CNAME: {a_answers.canonical_name})")

                        except dns.resolver.NoAnswer:
                            print(f"      → No A records for {mx_host}")
                        except dns.resolver.NXDOMAIN:
                            print(f"      → MX host {mx_host} does not exist")
                        except dns.resolver.NoNameservers:
                            print(f"      → No nameservers available for {mx_host}")
                        except Exception as e:
                            print(f"      → Error resolving {mx_host}: {e}")

                except dns.resolver.NoAnswer:
                    print(f"    → No MX records for {mx_domain}")
                except dns.resolver.NXDOMAIN:
                    print(f"    → Domain {mx_domain} does not exist")
                except Exception as e:
                    print(f"    → Error retrieving MX records: {e}")

    return ipv4_addresses


def ddd() -> None:
    answers = dns.resolver.resolve("pcbway.com", "TXT")
    rdata: dns.rdtypes.ANY.TXT.TXT
    for rdata in answers:
        print(f"{type(rdata)=} {rdata=}")
        print(f"Resolve response for pcbway.com TXT record : {rdata.to_text()=}")


def ipset_exists(ipset_instance: IPSet, name: str) -> bool:
    """Checks if an ipset with the given name exists."""
    try:
        # list() without parameters lists all ipsets
        all_ipsets = ipset_instance.list()
        for msg in all_ipsets:
            setname_attr = msg.get_attr("IPSET_ATTR_SETNAME")
            if setname_attr:
                setname = setname_attr.decode("utf-8") if isinstance(setname_attr, bytes) else setname_attr
                if setname == name:
                    return True
        return False
    except Exception:
        return False


def ipset_update_with_swap(
    srcname: str,
    ipv4_addr_or_net: List[str],
    do_actual_swap: bool = True,
    create_srcname_defaulttype: str | None = "hash:net",
) -> None:
    """
    Updates an ipset atomically using swap operation.

    Args:
        srcname: Name of the ipset to be updated
        entries: List of IP addresses/networks to be inserted into the ipset
        :param create_srcname_defaulttype:
    """
    ipset = IPSet()

    src_exists: bool = False

    try:
        # 1. Determine the type of the existing ipset
        msg_list = ipset.list(srcname)
        stype = None

        for msg in msg_list:
            print(f"{type(msg)=} {msg=}")

            # Get the set type from the attributes
            type_attr = msg.get_attr("IPSET_ATTR_TYPENAME")
            if type_attr:
                stype = type_attr.decode("utf-8") if isinstance(type_attr, bytes) else type_attr
                break

        if not stype:
            if create_srcname_defaulttype is None:
                raise ValueError(f"Could not determine type of ipset '{srcname}' and no default type was specified.")
            else:
                stype = create_srcname_defaulttype
                print(
                    f"SRC ipset with name {srcname} does not exist -> using default type {create_srcname_defaulttype}"
                )
        else:
            src_exists = True
            print(f"Determined ipset type: {stype}")

        # 2. Generate a random name for the temporary ipset
        random_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        temp_name = f"tmp_{srcname}_{random_suffix}"
        if src_exists:
            print(f"Temporary ipset: {temp_name}")
        else:
            temp_name = srcname

        # 3. Create the temporary ipset with the same type
        ipset.create(temp_name, stype=stype)
        if src_exists:
            print(f"Temporary ipset '{temp_name}' created")
        else:
            print(f"ipset '{temp_name}' created")

        # 4. Determine the etype based on the stype
        # For most hash:ip and hash:net sets we can use "net",
        # as it also accepts individual IPs (as /32 network)
        if "hash:ip" in stype or "hash:net" in stype:
            etype = "net"
        elif "bitmap:ip" in stype:
            etype = "ip"
        else:
            # Fallback: try to use the part after the colon
            etype = stype.split(":")[1] if ":" in stype else "ip"

        print(f"Using etype: {etype}")

        # 5. Add all entries to the temporary ipset
        for entry in ipv4_addr_or_net:
            if not ipset.test(temp_name, entry, etype=etype):
                ipset.add(temp_name, entry, etype=etype)
                print(f"  → Added: {entry}")
            else:
                print(f"  → Skipped: {entry}")

        if src_exists:
            print(f"Total of {len(ipv4_addr_or_net)} entries added to temporary ipset")
        else:
            print(f"Total of {len(ipv4_addr_or_net)} entries added to ipset")

        if do_actual_swap:
            if src_exists:
                # 6. Swap the temporary ipset with the source ipset
                ipset.swap(srcname, temp_name)
                print(f"ipsets '{srcname}' and '{temp_name}' swapped")

                # 7. Destroy the temporary ipset (which now contains the old data)
                ipset.destroy(temp_name)
                print(f"Temporary ipset '{temp_name}' destroyed")
        else:
            print("ACTUAL SWAP DISABLED!")

    except Exception as e:
        print(f"Error updating ipset: {e}")
        # Cleanup: Try to delete the temporary ipset if it exists
        try:
            if "temp_name" in locals() and src_exists:
                ipset.destroy(temp_name)
        except:
            pass
        raise
    finally:
        ipset.close()


def main() -> None:
    # Process command line arguments
    # sys.argv[0] is the script name, sys.argv[1:] are the arguments
    domains = sys.argv[1:] if len(sys.argv) > 1 else ["pcbway.com", "mail-notify.pcbway.com"]

    print(f"Processing {len(domains)} domain(s): {', '.join(domains)}\n")

    # Collect all IPv4 addresses for all domains
    all_ipv4_combined = []

    for domain in domains:
        print("\n" + "=" * 50 + "\n")
        print(f"Processing domain: {domain}")
        print("\n" + "=" * 50 + "\n")

        get_spf_records(domain)

        print("\n" + "=" * 50)
        print(f"Resolving SPF records to IPv4 addresses for {domain}")
        print("=" * 50 + "\n")

        domain_ipv4 = resolve_spf_to_ipv4(domain)

        print("\n" + "=" * 50)
        print(f"Found IPv4 addresses for {domain}: {len(domain_ipv4)}")
        print("=" * 50)
        for ip in domain_ipv4:
            print(f"  - {ip}")

        all_ipv4_combined.extend(domain_ipv4)

    print("\n" + "=" * 50)
    print(f"Total IPv4 addresses found (all domains): {len(all_ipv4_combined)}")
    print("=" * 50)
    for ip in all_ipv4_combined:
        print(f"  - {ip}")

    # Check if the user has root privileges
    if os.getuid() == 0:
        ipset_update_with_swap(
            "smtpallowlist", all_ipv4_combined, do_actual_swap=True, create_srcname_defaulttype="hash:net"
        )
    else:
        print("\n" + "=" * 50)
        print("WARNING: ipset update will be skipped!")
        print("Root privileges (UID 0) are required to update ipsets.")
        print(f"Current UID: {os.getuid()}")
        print("=" * 50)


if __name__ == "__main__":
    main()
