import 'package:flutter/material.dart';

abstract final class GaiaColors {
  static const ink = Color(0xFF17342A);
  static const forest = Color(0xFF234D3A);
  static const leaf = Color(0xFF3F7D58);
  static const lime = Color(0xFFC5E58A);
  static const canvas = Color(0xFFF6F8F1);
  static const muted = Color(0xFF718177);
  static const line = Color(0xFFE7EDE6);
  static const terracotta = Color(0xFFB66A3C);
  static const water = Color(0xFFDDECF1);
  static const soil = Color(0xFFF5E5D6);
  static const plant = Color(0xFFE5F0D4);
}

abstract final class GaiaSpacing {
  static const xs = 6.0;
  static const sm = 10.0;
  static const md = 16.0;
  static const lg = 22.0;
  static const xl = 28.0;
}

abstract final class GaiaRadius {
  static const card = 24.0;
  static const control = 16.0;
  static const pill = 40.0;
}

abstract final class GaiaMotion {
  static const quick = Duration(milliseconds: 220);
  static const standard = Duration(milliseconds: 360);
}
