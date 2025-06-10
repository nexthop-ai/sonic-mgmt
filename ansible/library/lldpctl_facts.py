#!/usr/bin/env python
# This ansible module is for gathering lldp facts from SONiC device.
# It takes two argument
# asic_instance_id :- Used to specify LLDP Instance in Multi-asic platforms
# skip_interface_pattern_list:- Used to specify interface pattern list to be skip for gathering lldp facts.
import re
import json
from ansible.module_utils.basic import AnsibleModule


def gather_lldp(module, lldpctl_docker_cmd, skip_interface_pattern_list):
    _, output, _ = module.run_command(lldpctl_docker_cmd)
    if not output:
        return {}

    try:
        # Parse the JSON output
        lldp_json = json.loads(output)

        # The objects are received in the hierarchy:
        # "lldp": {
        #     "interface" {
        #         "<ifname>": {
        #             "chassis": {
        #                 "<peer_name>": {
        #                     attrs....
        #                 }
        #             }
        #         }
        #     }
        # }
        # and stored as for easier access:
        # "lldp": {
        #     "<ifname>": {
        #         "<peer_name>": {
        #             "chassis": { "id": {}, "mgmt-ip": {} },
        #         }
        #     }
        #  }

        # Apply interface filtering if needed
        if 'lldp' in lldp_json and 'interface' in lldp_json['lldp']:
            # Transform the interface list into a dictionary with interface names as keys
            interface_dict = {}
            pattern = None
            if skip_interface_pattern_list:
                skip_interface_pattern_str = "(?:%s)" % '|'.join(skip_interface_pattern_list)
                pattern = re.compile(skip_interface_pattern_str)
            for interface_obj in lldp_json['lldp']['interface']:
                # Each interface object has a single key which is the interface name
                for interface_name, interface_data in interface_obj.items():
                    # Skip interfaces that match the pattern
                    if pattern and pattern.match(interface_name):
                        continue
                    # Add to our result dictionary
                    if interface_name not in interface_dict:
                        interface_dict[interface_name] = {}

                    # Get the neighbor name from the chassis data
                    if 'chassis' in interface_data:
                        chassis_name = next(iter(interface_data["chassis"]))
                        if chassis_name not in interface_dict[interface_name]:
                            interface_dict[interface_name][chassis_name] = {
                                 "chassis": interface_data["chassis"][chassis_name]
                            }

                        # Copy all the interface data to this neighbor, except chassis
                        for key, value in interface_data.items():
                            if key != "chassis":
                                interface_dict[interface_name][chassis_name][key] = value
            return interface_dict
        return lldp_json.get('lldp', {})
    except json.JSONDecodeError as e:
        module.fail_json(msg=f"Failed to parse lldpctl JSON output: {str(e)}")


def main():
    module = AnsibleModule(argument_spec=dict(
        asic_instance_id=dict(required=False, type='int', default=None),
        skip_interface_pattern_list=dict(
            required=False, type='list', default=None)
    ),
        supports_check_mode=False)

    m_args = module.params
    lldpctl_docker_cmd = "docker exec -i {} lldpctl -f json".format("lldp" + (
        str(m_args["asic_instance_id"]) if m_args["asic_instance_id"] is not None else ""))
    lldp_output = gather_lldp(
        module, lldpctl_docker_cmd, m_args["skip_interface_pattern_list"])
    try:
        data = {"lldpctl": lldp_output}
        module.exit_json(ansible_facts=data)
    except TypeError:
        module.fail_json(msg="lldpctl command failed")


if __name__ == '__main__':
    main()
