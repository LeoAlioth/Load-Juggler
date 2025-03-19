# Dynamic OCPP EVSE

Integration that simplifies making an OCPP supported EVSE dynamic.

If you have a smart meter or PV with consumptiopn monitoring, and an EVSE integrated into HA with the [OCPP integration](https://github.com/lbbrhzn/ocpp), this integration will take care of calculating and setting the charge current of the EVSE.

Following modes are available:
 - **Standard**: Charges as fast as possible according to the set import power limit
 - **Eco**: Charges at the default (6A), and speeds up if enough solar is available
 - **Solar**: Only charges when enough solar is available

In case the communication between HA and the EVSE fails, the EVSE will revert back to the default charge profile (6A, but can be set to 0 through configuration)
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

After installation, go to Settings -> Add Integration and search for `Dynamic OCPP EVSE`
Most of the fields should auto populate, but can be changed if needed. If any setting is unclear, please let me know.
