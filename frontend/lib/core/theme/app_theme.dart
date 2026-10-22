import 'package:flutter/material.dart';
import 'app_tokens.dart';

abstract final class GaiaTheme {
  static ThemeData light() {
    final scheme = ColorScheme.fromSeed(seedColor: GaiaColors.leaf, brightness: Brightness.light);
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: GaiaColors.canvas,
      fontFamily: 'Arial',
      appBarTheme: const AppBarTheme(backgroundColor: Colors.transparent, foregroundColor: GaiaColors.ink, elevation: 0),
      cardTheme: CardThemeData(color: Colors.white, elevation: 0, shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(GaiaRadius.card))),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: const Color(0xFFF3F6F1),
        border: OutlineInputBorder(borderSide: BorderSide.none, borderRadius: BorderRadius.circular(GaiaRadius.control)),
      ),
    );
  }
}
