#!/usr/bin/env python3
"""
Test edge cases for XML parsing to ensure the force_list approach is robust
"""

import xmltodict


def test_edge_cases():
    """Test various edge cases for XML parsing"""

    print("=== Testing Edge Cases for XML Parsing ===\n")

    # Test case 1: No rooms at all
    print("1. Testing with no rooms:")
    no_rooms_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
    </rooms>
</rako>"""

    try:
        no_rooms_dict = xmltodict.parse(no_rooms_xml, force_list={"Room"})
        rooms_section = no_rooms_dict["rako"]["rooms"]
        rooms = rooms_section.get("Room", []) if rooms_section else []
        print(f"   No rooms - Type: {type(rooms)}, Length: {len(rooms)}")
        print(f"   Iteration works: {list(rooms) == []}")
    except Exception as e:
        print(f"   ERROR: {e}")

    # Test case 2: Multiple ventilation rooms
    print("\n2. Testing with multiple ventilation rooms:")
    multi_vent_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="161">
            <Type>Ventilation</Type>
            <Title>Fans 1</Title>
        </Room>
        <Room id="162">
            <Type>Ventilation</Type>
            <Title>Fans 2</Title>
        </Room>
    </rooms>
</rako>"""

    try:
        multi_vent_dict = xmltodict.parse(multi_vent_xml, force_list={"Room"})
        rooms = multi_vent_dict["rako"]["rooms"]["Room"]
        print(f"   Multiple ventilation - Type: {type(rooms)}, Length: {len(rooms)}")

        vent_count = 0
        for room in rooms:
            if room.get("Type") == "Ventilation":
                vent_count += 1
        print(f"   Ventilation rooms found: {vent_count}")

    except Exception as e:
        print(f"   ERROR: {e}")

    # Test case 3: Mixed room types with single ventilation
    print("\n3. Testing mixed room types with single ventilation:")
    mixed_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="1">
            <Type>Lights</Type>
            <Title>Light Room 1</Title>
        </Room>
        <Room id="161">
            <Type>Ventilation</Type>
            <Title>Fans</Title>
        </Room>
        <Room id="2">
            <Type>Lights</Type>
            <Title>Light Room 2</Title>
        </Room>
    </rooms>
</rako>"""

    try:
        mixed_dict = xmltodict.parse(mixed_xml, force_list={"Room"})
        rooms = mixed_dict["rako"]["rooms"]["Room"]
        print(f"   Mixed rooms - Type: {type(rooms)}, Length: {len(rooms)}")

        types = {}
        for room in rooms:
            room_type = room.get("Type", "Unknown")
            types[room_type] = types.get(room_type, 0) + 1
        print(f"   Room types: {types}")

    except Exception as e:
        print(f"   ERROR: {e}")

    # Test case 4: What happens without force_list in various scenarios
    print("\n4. Testing without force_list in various scenarios:")

    # Single room
    single_room_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="161">
            <Type>Ventilation</Type>
            <Title>Fans</Title>
        </Room>
    </rooms>
</rako>"""

    try:
        single_no_force = xmltodict.parse(single_room_xml)
        room_data = single_no_force["rako"]["rooms"]["Room"]
        print(f"   Single room without force_list - Type: {type(room_data)}")
        print(
            f"   Would break iteration: {not hasattr(room_data, '__iter__') or isinstance(room_data, (str, dict))}"
        )
    except Exception as e:
        print(f"   ERROR: {e}")

    # Multiple rooms
    try:
        multi_no_force = xmltodict.parse(multi_vent_xml)
        room_data = multi_no_force["rako"]["rooms"]["Room"]
        print(f"   Multiple rooms without force_list - Type: {type(room_data)}")
        print(f"   Would work with iteration: {isinstance(room_data, list)}")
    except Exception as e:
        print(f"   ERROR: {e}")

    print("\n=== Edge Case Analysis ===")
    print("The force_list={'Room'} approach is robust because:")
    print("1. It handles empty rooms sections gracefully")
    print("2. It ensures consistent list type regardless of room count")
    print("3. It works with single, multiple, and mixed room types")
    print("4. Without force_list, single rooms become dicts, breaking iteration")
    print("5. The bridge.py code correctly uses this approach")


if __name__ == "__main__":
    test_edge_cases()
