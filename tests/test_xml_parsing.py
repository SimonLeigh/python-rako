#!/usr/bin/env python3
"""
Test script to analyze XML parsing behavior with xmltodict.parse() and force_list={"Room"}
This tests the specific issue at line 236 in bridge.py where single room instances
might not be handled correctly.
"""

import json
from pathlib import Path

import xmltodict


def test_xml_parsing():
    """Test the XML parsing logic from bridge.py line 236"""

    # Read the XML file
    xml_path = Path("tests/resources/rako3.xml")
    with xml_path.open(encoding="utf-8") as f:
        xml_content = f.read()

    print("=== Testing XML Parsing Logic ===")
    print(f"XML file: {xml_path}")
    print()

    # Parse using the current approach from bridge.py:236
    print("1. Parsing with xmltodict.parse(xml, force_list={'Room'})")
    xml_dict = xmltodict.parse(xml_content, force_list={"Room"})

    # Test accessing the rooms
    print("2. Testing access to xml_dict['rako']['rooms']['Room']")
    rooms = xml_dict["rako"]["rooms"]["Room"]

    print(f"   Type of rooms: {type(rooms)}")
    print(f"   Length of rooms: {len(rooms)}")
    print()

    # Count rooms by type
    room_counts = {}
    for room in rooms:
        room_type = room.get("Type", "Unknown")
        room_counts[room_type] = room_counts.get(room_type, 0) + 1

    print("3. Room counts by type:")
    for room_type, count in room_counts.items():
        print(f"   {room_type}: {count}")
    print()

    # Test the specific ventilation room (id=161)
    print("4. Testing ventilation room access:")
    ventilation_rooms = [room for room in rooms if room.get("Type") == "Ventilation"]

    if ventilation_rooms:
        vent_room = ventilation_rooms[0]
        print(f"   Found ventilation room: ID={vent_room['@id']}, Title={vent_room['Title']}")
        print(f"   Ventilation room structure: {json.dumps(vent_room, indent=2)}")
    else:
        print("   No ventilation rooms found!")
    print()

    # Test what happens with a minimal XML with just one room
    print("5. Testing with minimal XML (single room):")
    minimal_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rako>
    <rooms>
        <Room id="161">
            <Type>Ventilation</Type>
            <Title>Fans</Title>
            <Channel id="1">
                <type>switch</type>
                <Name>Fans</Name>
                <Levels>FFBF7F3F000000000000000000000000</Levels>
            </Channel>
        </Room>
    </rooms>
</rako>"""

    minimal_dict = xmltodict.parse(minimal_xml, force_list={"Room"})
    minimal_rooms = minimal_dict["rako"]["rooms"]["Room"]

    print(f"   Type of minimal rooms: {type(minimal_rooms)}")
    print(f"   Length of minimal rooms: {len(minimal_rooms)}")
    print(f"   First room type: {minimal_rooms[0].get('Type')}")
    print()

    # Test without force_list to see the difference
    print("6. Testing without force_list (potential issue):")
    no_force_dict = xmltodict.parse(minimal_xml)
    no_force_rooms = no_force_dict["rako"]["rooms"]["Room"]

    print(f"   Type without force_list: {type(no_force_rooms)}")
    print(f"   Content: {json.dumps(no_force_rooms, indent=2)}")
    print()

    # Test the bridge.py logic simulation
    print("7. Simulating bridge.py logic (line 237):")
    print("   for room in xml_dict['rako']['rooms']['Room']:")

    try:
        for i, room in enumerate(xml_dict["rako"]["rooms"]["Room"]):
            room_id = int(room["@id"])
            room_type = room.get("Type", "Lights")
            room_title = room["Title"]

            if i < 3:  # Show first 3 rooms
                print(f"   Room {i}: ID={room_id}, Type={room_type}, Title={room_title}")
            elif i == 3:
                print(f"   ... (showing first 3 of {len(xml_dict['rako']['rooms']['Room'])} rooms)")
                break

        # Show the ventilation room specifically
        for room in xml_dict["rako"]["rooms"]["Room"]:
            if room.get("Type") == "Ventilation":
                room_id = int(room["@id"])
                room_type = room.get("Type", "Lights")
                room_title = room["Title"]
                print(f"   Ventilation Room: ID={room_id}, Type={room_type}, Title={room_title}")
                break

    except Exception as e:
        print(f"   ERROR: {e}")
        print("   This indicates a potential issue with the parsing logic!")

    print()
    print("=== Analysis Summary ===")
    print(f"- Total rooms in XML: {len(rooms)}")
    print(f"- Lights rooms: {room_counts.get('Lights', 0)}")
    print(f"- Ventilation rooms: {room_counts.get('Ventilation', 0)}")
    print(f"- force_list={'Room'} ensures rooms is always a list: {isinstance(rooms, list)}")
    print(f"- Single room XML with force_list works correctly: {isinstance(minimal_rooms, list)}")
    print(f"- Without force_list, single room becomes dict: {isinstance(no_force_rooms, dict)}")
    print()
    print("CONCLUSION:")
    print("The current code at bridge.py:236 using force_list={'Room'} is CORRECT.")
    print("It properly handles both single and multiple room scenarios.")
    print("Without force_list, a single room would be a dict, breaking the iteration.")


if __name__ == "__main__":
    test_xml_parsing()
