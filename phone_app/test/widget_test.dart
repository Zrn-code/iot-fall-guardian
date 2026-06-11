// Smoke test for the Guardian Console app.
//
// The app boots into a RootGate that shows a loading spinner while it restores
// any stored ThingsBoard session, then falls back to the login screen. This
// test just verifies the app builds and renders that initial frame.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:phone_app/main.dart';

void main() {
  testWidgets('Guardian app boots into RootGate', (WidgetTester tester) async {
    await tester.pumpWidget(const GuardianApp());

    // First frame while the stored session is being restored.
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
  });
}
