# Green Acres farm layout and sensor guide

## Layout
- North plot: 0.5 acre of tomato, 6 rows, drip irrigated.
- South beds: 0.3 acre of chilli, 8 raised beds, hose irrigated.
- East corner: cattle shed and the biogas digester.
- West fence: okra border strip.

## What the rover measures
- Air temperature and relative humidity (DHT11). Accuracy is about plus or minus 2 degrees Celsius and plus or
  minus 5 percent humidity.
- Soil moisture (capacitive probe on the servo arm). Only valid while the rover is parked with the arm lowered.
  Dry air is about 0 percent and a glass of water is about 100 percent after calibration.
- MQ5 and MQ6 gas sensors. They give a relative index from 0 to 1023, not a concentration in ppm. MQ5 responds
  to methane and natural gas, MQ6 to LPG and butane. A baseline of roughly 150 to 300 is normal for this field.

## How to read the values
- Humidity above 85 percent for several hours: higher fungal disease pressure.
- Temperature above 38 degrees Celsius: heat stress, flowers may drop.
- MQ5 above 500 near the biogas unit in the evening: expected after the digester is fed.
- MQ readings above 500 anywhere else in the field: check the area for a gas source.
