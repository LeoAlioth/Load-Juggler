# Dynamic OCPP EVSE

Integration that simplifies making an OCPP supported EVSE dynamic.

If you have a smart meter or PV with consumption monitoring, and an EVSE integrated into HA with the [OCPP integration](https://github.com/lbbrhzn/ocpp), this integration will take care of calculating and setting the charge current of the EVSE.

## Table of Contents

- [Features](#features)
- [Charging Modes](#charging-modes)
- [Battery System Support](#battery-system-support)
- [Installation](#installation)
- [Configuration](#configuration)
- [Supported Equipment](#supported-equipment)
- [Testing and Feedback](#testing-and-feedback)

## Features

- **Dynamic charging control** based on available power and solar generation
- **Multiple charging modes** to suit different needs and scenarios
- **Battery system integration** for optimal energy management
- **Automatic inverter detection** for popular brands (SolarEdge, Solarman/Deye)
- **Power-to-current conversion** for systems that only provide power readings
- **Failsafe operation** - EVSE reverts to default profile if communication fails

## Charging Modes

The integration offers four distinct charging modes:

- **Standard**: Charges as fast as possible according to the set import power limit. Ideal for maximum charging speed when grid power usage is not a concern.

- **Eco**: Charges at the minimum current and increases charging speed when sufficient solar power is available. Balances charging speed with solar utilization.

- **Solar**: Only charges when enough solar power is available, minimizing grid consumption. Perfect for maximizing self-consumption of solar energy.

- **Excess**: Advanced mode that starts charging only when solar export exceeds a configurable threshold. This mode is designed for systems with high solar generation that want to utilize excess power for EV charging while maintaining battery charging priority. The charging continues for 15 minutes even if export drops below the threshold, providing stable charging sessions.

## Battery System Support

The integration includes comprehensive battery system support:

- **Battery SOC monitoring** - tracks current battery state of charge
- **SOC target management** - respects minimum battery charge levels
- **Intelligent discharge control** - allows battery power to supplement EV charging when SOC is above target
- **Charge/discharge power limits** - configurable maximum battery charge and discharge rates
- **Grid charging control** - optional switch to allow/disallow charging from grid power

Battery integration works seamlessly with all charging modes, ensuring optimal energy management between solar generation, battery storage, and EV charging.

## Installation

**Method 1 _(easiest)_:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LeoAlioth&repository=Dynamic_OCPP_EVSE&category=integration)

**Method 2:**

1. **Download the Custom Component**
   - Download the files from the repository.

2. **Copy to Your Custom Components Directory**
   - Copy the downloaded folder `dynamic_ocpp_evse` into the `custom_components/dynamic_ocpp_evse` directory in your Home Assistant configuration directory.

3. **Restart Home Assistant**
   - Restart your Home Assistant instance to load the new component.

## Configuration

### Import power limit helper

Create a template sensor, that holds the maximum import power. You will need it in the configuration steps. I Recommend the name to contain "Power Limit" so it gets auto selected during configuration.

- The template can be whatever you want, for s simple static example of 6 kW: {{ 6000 }}
- Unit of measurement: W
- Device class: Power
- State class: Measurement

( Using an input_number is not yet possible but planned for future release)

### Adding integration

After installation, go to Settings -> Add Integration and search for `Dynamic OCPP EVSE`

The integration will automatically detect and suggest appropriate sensors based on your system:

### Configuration options

1. **Phase Current/Power Sensors**: The integration will automatically detect phase sensors from supported inverters
2. **EVSE Sensors**: Select your EVSE current import and offered sensors from the OCPP integration
3. **Power Limits**: Configure your maximum import power and main breaker rating
4. **Battery Configuration** (optional): Set up battery SOC, power sensors, and charge/discharge limits
5. **Charging Parameters**: Set minimum and maximum charging currents

Most fields should auto-populate during setup. If they do not, please report that, with the ids of entities that should be selected, so i can improve searching.

**Important**: Generally, the EVSE has some charge profiles set, and those might not be compatible with the ones this integration creates. After first install, call the reset_ocpp_evse action via the **Reset OCPP EVSE** button.

## Supported equipment

### power meters / inverters

While technically you can use any home assistant entity, the integration also automatically detects sensors for some setups:

- SolarEdge
- Deye - External CTs
- Deye - Internal CTs
- Solar Assistant - Grid Power (Individual Phases)

The integration supports both current (A) and power (W) sensors, automatically converting power to current using the configured phase voltage.

## Testing and Feedback

🧪 **Looking for Testers!** 🧪

This integration is actively being developed and improved. I'm looking for users to test it with different setups and provide feedback.

**Especially interested in testing with:**

- Different inverter/meter brands and models
- Battery systems (different brands and configurations)
- Multi-phase vs single-phase installations
- Different grid configurations and power limits

**How to help:**

- Install and test the integration with your setup
- Report any issues or unexpected behavior via [GitHub Issues](https://github.com/LeoAlioth/Dynamic_OCPP_EVSE/issues)
- Share your configuration and experiences
- Suggest improvements or new features

Your testing and feedback help make this integration better for everyone! 🚗⚡
