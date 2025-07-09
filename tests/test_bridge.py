from python_rako.bridge import Bridge
from python_rako.model import (
    BridgeInfo,
    ChannelLight,
    ChannelVentilation,
    RoomLight,
    RoomVentilation,
)


def test_get_lights_from_discovery_xml(rako_xml):
    lights = Bridge.get_lights_from_discovery_xml(rako_xml)

    expected_lights = [
        RoomLight(room_id=5, room_title="Living Room", channel_id=0),
        ChannelLight(
            room_id=5,
            room_title="Living Room",
            channel_id=1,
            channel_type="Default",
            channel_name="Downlights",
            channel_levels="FF347F3F000000000000000000000000",
        ),
        ChannelLight(
            room_id=5,
            room_title="Living Room",
            channel_id=2,
            channel_type="Default",
            channel_name="Kitchen Downlights",
            channel_levels="FF337F3F000000000000000000000000",
        ),
        RoomLight(room_id=9, room_title="Bedroom 1", channel_id=0),
        ChannelLight(
            room_id=9,
            room_title="Bedroom 1",
            channel_id=1,
            channel_type="Default",
            channel_name="Downlights",
            channel_levels="FFBF7F25000000000000000000000000",
        ),
        ChannelLight(
            room_id=9,
            room_title="Bedroom 1",
            channel_id=2,
            channel_type="Default",
            channel_name="LED",
            channel_levels="FFBF7F00000000000000000000000000",
        ),
    ]
    assert list(lights) == expected_lights


def test_get_bridge_info_from_discovery_xml(rako_xml):
    info = Bridge.get_bridge_info_from_discovery_xml(rako_xml)

    expected_info = BridgeInfo(
        version="2.4.0 RA",
        buildDate="Nov 17 2017 10:01:01",
        hostName="RAKOBRIDGE",
        hostIP="someip",
        hostMAC="somemac",
        hwStatus="05",
        dbVersion="-31",
        requirepassword=None,
        passhash="NAN",
        charset="UTF-8",
    )
    assert info == expected_info


def test_get_bridge_info_from_discovery_xml2(rako_xml2):
    info = Bridge.get_bridge_info_from_discovery_xml(rako_xml2)

    expected_info = BridgeInfo(
        version=None,
        buildDate=None,
        hostName=None,
        hostIP=None,
        hostMAC=None,
        hwStatus=None,
        dbVersion=None,
        requirepassword=None,
        passhash="NAN",
        charset=None,
    )
    assert info == expected_info


def test_get_lights_from_discovery_xml2(rako_xml2):
    lights = Bridge.get_lights_from_discovery_xml(rako_xml2)

    expected_lights = [
        RoomLight(room_id=112, room_title="Bedroom 1", channel_id=0),
        ChannelLight(
            room_id=112,
            room_title="Bedroom 1",
            channel_id=1,
            channel_type="Default",
            channel_name="Ceiling Light",
            channel_levels="FFBF7F3F000000000000000000000000",
        ),
        RoomLight(room_id=108, room_title="Master Ensuite", channel_id=0),
    ]

    assert list(lights) == expected_lights


def test_get_ventilation_from_discovery_xml(rako_xml3):
    ventilation_devices = Bridge.get_devices_from_discovery_xml(
        rako_xml3, "Ventilation"
    )

    expected_ventilation = [
        RoomVentilation(room_id=161, room_title="Fans", channel_id=0),
        ChannelVentilation(
            room_id=161,
            room_title="Fans",
            channel_id=1,
            channel_type="switch",
            channel_name="Fans",
            channel_levels="FFBF7F3F000000000000000000000000",
        ),
    ]

    ventilation_list = [
        dev
        for dev in ventilation_devices
        if isinstance(dev, (RoomVentilation, ChannelVentilation))
    ]
    assert ventilation_list == expected_ventilation


def test_ventilation_command_compatibility():
    """Test that ventilation can use the same commands as lighting"""
    from python_rako.model import CommandLevelHTTP, CommandSceneHTTP

    # Test that ventilation room can use lighting commands
    room_scene_command = CommandSceneHTTP(room=161, channel=0, scene=1)
    assert room_scene_command.as_params() == {"room": 161, "ch": 0, "sc": 1}

    # Test that ventilation channel can use lighting level commands
    channel_level_command = CommandLevelHTTP(room=161, channel=1, level=128)
    assert channel_level_command.as_params() == {"room": 161, "ch": 1, "lev": 128}


def test_get_all_devices_from_discovery_xml(rako_xml3):
    """Test discovering all device types from XML"""
    all_devices = list(Bridge.get_all_devices_from_discovery_xml(rako_xml3))

    # Should contain both lights and ventilation
    lights = [dev for dev in all_devices if isinstance(dev, (RoomLight, ChannelLight))]
    ventilation = [
        dev
        for dev in all_devices
        if isinstance(dev, (RoomVentilation, ChannelVentilation))
    ]

    # Should have many lights and 2 ventilation devices
    assert len(lights) > 10  # rako3.xml has many light rooms
    assert len(ventilation) == 2  # 1 RoomVentilation + 1 ChannelVentilation

    # Check specific ventilation devices are included
    ventilation_room = next(
        (dev for dev in ventilation if isinstance(dev, RoomVentilation)), None
    )
    assert ventilation_room is not None
    assert ventilation_room.room_id == 161
    assert ventilation_room.room_title == "Fans"


def test_get_devices_with_specific_types(rako_xml3):
    """Test discovering specific device types using list parameter"""
    # Test with list of device types
    lights_only = list(Bridge.get_devices_from_discovery_xml(rako_xml3, ["Lights"]))
    ventilation_only = list(
        Bridge.get_devices_from_discovery_xml(rako_xml3, ["Ventilation"])
    )
    both_types = list(
        Bridge.get_devices_from_discovery_xml(rako_xml3, ["Lights", "Ventilation"])
    )

    # Verify filtering works correctly
    assert all(isinstance(dev, (RoomLight, ChannelLight)) for dev in lights_only)
    assert all(
        isinstance(dev, (RoomVentilation, ChannelVentilation))
        for dev in ventilation_only
    )
    assert len(both_types) == len(lights_only) + len(ventilation_only)


def test_get_devices_backward_compatibility(rako_xml3):
    """Test that existing string parameter still works"""
    # Test backward compatibility with string parameter
    lights_str = list(Bridge.get_devices_from_discovery_xml(rako_xml3, "Lights"))
    lights_list = list(Bridge.get_devices_from_discovery_xml(rako_xml3, ["Lights"]))

    # Should return the same devices
    assert lights_str == lights_list

    # Test ventilation backward compatibility
    ventilation_str = list(
        Bridge.get_devices_from_discovery_xml(rako_xml3, "Ventilation")
    )
    ventilation_list = list(
        Bridge.get_devices_from_discovery_xml(rako_xml3, ["Ventilation"])
    )

    assert ventilation_str == ventilation_list


def test_get_devices_all_parameter_variants(rako_xml3):
    """Test different ways to get all devices"""
    all_devices_none = list(Bridge.get_devices_from_discovery_xml(rako_xml3, None))
    all_devices_all = list(Bridge.get_devices_from_discovery_xml(rako_xml3, "All"))
    all_devices_method = list(Bridge.get_all_devices_from_discovery_xml(rako_xml3))

    # All should return the same devices
    assert all_devices_none == all_devices_all == all_devices_method

    # Should contain both lights and ventilation
    assert any(isinstance(dev, (RoomLight, ChannelLight)) for dev in all_devices_none)
    assert any(
        isinstance(dev, (RoomVentilation, ChannelVentilation))
        for dev in all_devices_none
    )
