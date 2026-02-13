# Improvements

This document is used for keeping notes of ideas for future implementations in no particular order. As long as the developer does not expicitly say to start implementing them, you can just use this as a reference for what might come, if that has any effect on current decisions. This will also keep any discussions about ideas. This way you can plan them easier with the developer.

## Supports for chargers with multiple plugs
pretty self explanatory

## Moving charge mode selection from the site to the individual evse
This might be really hard to implement, and also has some not yet known design decisions, specifically about the priority order. As long as the priority follows the charge modes, so a higher priority charger has a "faster" charge mode selected, things are fine. But lets say we use priority mode, where charger 1 is eco, and charger 2 is Standard?  if very little power is available, the 1st one should start charging first, but if there is no sun does that mean that the 2nd charger, even though it is lower priority, would charge at more than minimum speed while the 1st one stays at the minimum? And having lets say solar and standard combined might be even harder do define the behaviour for. Claude, please add some notes and possible soluitions with examples to this for further discussion.

## Adding support for "dumb" evse - smart sockets
While not many households have multiple smart evses, lots have for example a 3 phase smart evse, and a granny charger which could be used on a smart plug/relay. (assumig proper current ratings). Plugs with and without power monitoring should be considered. Same as for evse, phase configuration needs to be done. Then for plugs without power monitoring, a user should assign the power rating of the device plugged in. This is then considered as the minimum and maximum that the device can take, and if the minimum is reahed, it should switch it on. For plugs with power monitoring, the actual power draw should be measured, remembed, and kept up to date during the time it is on.

## Making this a general load management project
Same spirit as the smart plugs, but for thermostats. Be it hot water tanks or HVAC. While not usefull with standard or eco modes necessarily (this is why charge mode being a device level select not a site level select would be nice), using it with especially excess mode would be handy. A hot water boiler, would have a low priority in excess mode. in normal operation, it would just keep the thermostat at lets say 45C. But when excess mode allocates available power, it would bump that to 70c to make use of excess power. Same could be done for HVAC, just with smaller offset from the default temperature. Overcooling or overheating the place for a degree or two when excess power is available.

## Adding an entity selection for actual solar power in the config_flow
This does not seem necessary, and the info is not necessarily available at all sites. So we can't rely on it. But id like to discuss if having this info provided instead of derived would enable us adding any better features.
