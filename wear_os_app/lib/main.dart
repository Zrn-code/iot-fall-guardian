import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:sensors_plus/sensors_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:vibration/vibration.dart';

void main() {
  runApp(const WearAsnApp());
}

class WearAsnApp extends StatelessWidget {
  const WearAsnApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Guardian Watch',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(
        useMaterial3: true,
      ).copyWith(scaffoldBackgroundColor: Colors.black),
      home: const RecorderScreen(),
    );
  }
}

enum WearStage { monitoring, error, alerting, sosSent, acked, called }

enum InferTrigger { periodic, fall }

class RecorderScreen extends StatefulWidget {
  const RecorderScreen({super.key});
  @override
  State<RecorderScreen> createState() => _RecorderScreenState();
}

class _RecorderScreenState extends State<RecorderScreen> {
  static const String _defaultEvaluateUrl =
      'http://140.113.123.43:8000/api/infer';
  // Wearer device token (type=wearer). Intentionally empty in source: set it at
  // runtime via the in-app settings dialog (stored in shared_preferences), or
  // read it from data/worker_tokens.json after running provision_thingsboard.py.
  static const String _defaultTbToken = '';
  static const MethodChannel _hrChannel = MethodChannel('wear_os/heart_rate');

  // Continuous monitoring: rolling IMU window + inference cadence.
  static const int _imuHz = 50;
  static const int _imuCap = 300; // 6 s @ 50 Hz rolling window
  static const int _hrCap = 12; // keep ~last 12 HR samples (~1/s)
  static const int _minInferSamples = 16;
  static const Duration _inferEvery = Duration(seconds: 5);

  WearStage _stage = WearStage.monitoring;
  String _msg = '';
  String _evaluateUrl = _defaultEvaluateUrl;
  String _workerId = 'W-001';
  String _tbToken = '';

  final List<Map<String, dynamic>> _imuBuffer = [];
  final List<Map<String, dynamic>> _hrBuffer = [];
  Map<String, double> _lastAcc = {'x': 0, 'y': 0, 'z': 1};
  Map<String, double> _lastGyro = {'x': 0, 'y': 0, 'z': 0};

  StreamSubscription? _accSub;
  StreamSubscription? _gyroSub;
  StreamSubscription<Position>? _positionSub;
  Timer? _sampleTimer;
  Timer? _inferTimer;
  double _streamStart = 0;
  bool _started = false;
  bool _inferInFlight = false;
  double? _lastBgBpm;
  Position? _latestPosition;
  DateTime? _lastPosAt;
  DateTime? _lastHeartbeatPostAt;
  String? _lastHeartbeatError;

  // Device-state telemetry streamed to TB every 5 s alongside HR/GPS.
  int? _batteryPct; // watch battery 0-100 (BatteryManager via method channel)
  bool? _charging;
  DateTime? _lastHrAt; // last valid HR sample -> proxy for "is the watch worn"
  // Health Services only reports HR with skin contact, so a recent HR sample
  // means the watch is on the wrist.
  static const Duration _wornTimeout = Duration(seconds: 30);

  // v3 situation result + SOS countdown
  String? _situation;
  double? _situationConf;
  Timer? _alertCountdownTimer;
  int _alertCountdown = 0;
  bool _freefallSeen = false;
  static const int _alertCountdownSeconds = 15;
  // local fall detection (g): free-fall dip then impact spike
  static const double _freefallG = 0.45;
  static const double _impactG = 1.8;

  @override
  void initState() {
    super.initState();
    _hrChannel.setMethodCallHandler(_onHrCall);
    _loadPrefs();
    _startMonitoring();
  }

  Future<dynamic> _onHrCall(MethodCall call) async {
    final args = call.arguments;
    switch (call.method) {
      case 'onHeartRate':
        double? bpm;
        if (args is Map) {
          bpm = (args['bpm'] as num?)?.toDouble();
          if (bpm != null) {
            // Continuous monitoring: always buffer HR (no recording session).
            // Trim by count, not the platform timestamp (unit is untrusted).
            _hrBuffer.add({
              'timestamp': args['timestamp'],
              'bpm': bpm,
              'accuracy': args['accuracy'],
            });
            if (_hrBuffer.length > _hrCap) _hrBuffer.removeAt(0);
          }
        } else if (args is num) {
          bpm = args.toDouble();
        }
        if (bpm != null) {
          _lastHrAt = DateTime.now(); // worn proxy: HR needs skin contact
          if (mounted) setState(() => _lastBgBpm = bpm);
        }
        break;
    }
  }

  /// Start the always-on guardian: HR + GPS + IMU streams plus the periodic
  /// inference/heartbeat tick. Runs exactly once for the app's lifetime.
  Future<void> _startMonitoring() async {
    if (_started) return;
    _started = true;

    await _hrChannel.invokeMethod('requestHeartRatePermission');
    await _hrChannel.invokeMethod('startHeartRate');

    var perm = await Geolocator.checkPermission();
    if (perm == LocationPermission.denied) {
      perm = await Geolocator.requestPermission();
    }
    if (perm != LocationPermission.denied &&
        perm != LocationPermission.deniedForever) {
      _positionSub = Geolocator.getPositionStream(
        locationSettings: AndroidSettings(
          accuracy: LocationAccuracy.best,
          distanceFilter: 0,
          intervalDuration: const Duration(seconds: 1),
          forceLocationManager: true,
        ),
      ).listen((pos) {
        _latestPosition = pos;
        _lastPosAt = DateTime.now();
      }, onError: (_) {});
      // Seed an immediate fix so lat/lon is present from the first tick instead
      // of waiting for the stream's first emission (or never, if it stalls).
      Geolocator.getLastKnownPosition().then((p) {
        if (p != null && _latestPosition == null) {
          _latestPosition = p;
          _lastPosAt = DateTime.now();
        }
      }).catchError((_) {});
    }

    _startImuStream();

    _inferTimer = Timer.periodic(_inferEvery, (_) => _monitorTick());
    _monitorTick();
  }

  /// Subscribe to accel + gyro once and stream samples into a rolling window.
  /// The free-fall -> impact detector runs on every sample for the app's life.
  void _startImuStream() {
    _streamStart = DateTime.now().millisecondsSinceEpoch / 1000.0;
    _accSub = accelerometerEventStream(
      samplingPeriod: const Duration(milliseconds: 20),
    ).listen((e) {
      _lastAcc = {
        'x': e.x / 9.80665,
        'y': e.y / 9.80665,
        'z': e.z / 9.80665,
      };
    });
    _gyroSub = gyroscopeEventStream(
      samplingPeriod: const Duration(milliseconds: 20),
    ).listen((e) {
      _lastGyro = {'x': e.x, 'y': e.y, 'z': e.z};
    });

    _sampleTimer = Timer.periodic(const Duration(milliseconds: 20), (t) {
      final ts = _streamStart + t.tick * 0.02;
      _imuBuffer.add({
        'timestamp': ts,
        'acc_x': _lastAcc['x'],
        'acc_y': _lastAcc['y'],
        'acc_z': _lastAcc['z'],
        'gyro_x': _lastGyro['x'],
        'gyro_y': _lastGyro['y'],
        'gyro_z': _lastGyro['z'],
      });
      // Rolling trim: keep only the last _imuCap samples (~6 s).
      if (_imuBuffer.length > _imuCap) {
        _imuBuffer.removeRange(0, _imuBuffer.length - _imuCap);
      }
      // Auto fall trigger: a free-fall dip (~0 g) followed by an impact spike.
      final ax = _lastAcc['x'] ?? 0, ay = _lastAcc['y'] ?? 0, az = _lastAcc['z'] ?? 0;
      final mag = sqrt(ax * ax + ay * ay + az * az);
      if (mag < _freefallG) {
        _freefallSeen = true;
      } else if (_freefallSeen && mag > _impactG) {
        _freefallSeen = false;
        _onFallDetected();
      }
    });
  }

  /// Periodic 5 s tick: run inference on the rolling window when idle-monitoring,
  /// otherwise keep the dashboard heartbeat alive, then poll for guardian downlink.
  Future<void> _monitorTick() async {
    // Refresh device-state telemetry so every 5 s post carries battery + GPS.
    await _refreshBattery();
    _ensureGpsFresh();
    final inEscalation = _stage == WearStage.alerting ||
        _stage == WearStage.sosSent ||
        _stage == WearStage.acked ||
        _stage == WearStage.called;
    if (_inferInFlight) {
      // An inference is mid-flight; it posts telemetry itself. Just poll.
    } else if (!inEscalation && _imuBuffer.length >= _minInferSamples) {
      await _runInference(InferTrigger.periodic);
    } else {
      await _postHeartbeatOnly();
    }
    // Downlink: poll TB shared attributes for a guardian command (B4).
    await _pollGuardianCommand();
  }

  /// Minimal heartbeat used while escalating (or before the window fills): keep
  /// the dashboard's 5 s active=true + HR/GPS cadence without running inference.
  Future<void> _postHeartbeatOnly() async {
    try {
      final ok = await _postTb({'active': true});
      if (!mounted) return;
      if (ok) {
        setState(() {
          _lastHeartbeatPostAt = DateTime.now();
          _lastHeartbeatError = null;
        });
      } else {
        setState(() => _lastHeartbeatError = _tbToken.isEmpty ? 'no_tb' : 'tb_err');
      }
    } on TimeoutException {
      if (mounted) setState(() => _lastHeartbeatError = 'timeout');
    } catch (e) {
      if (!mounted) return;
      final msg = e.toString();
      final tag = (msg.contains('Failed host lookup') ||
              msg.contains('Connection refused') ||
              msg.contains('SocketException'))
          ? 'net'
          : 'err';
      setState(() => _lastHeartbeatError = tag);
    }
  }

  /// "Is the watch being worn" — proxied from HR contact (Health Services only
  /// emits HR with skin contact, so a recent valid sample means on-wrist).
  bool get _worn {
    final t = _lastHrAt;
    return t != null && DateTime.now().difference(t) < _wornTimeout;
  }

  /// Pull the current battery level/charging from the native side (no plugin).
  Future<void> _refreshBattery() async {
    try {
      final d = await _hrChannel.invokeMethod('batteryStatus');
      if (d is Map) {
        final lvl = (d['level'] as num?)?.toInt();
        if (lvl != null && lvl >= 0 && lvl <= 100) _batteryPct = lvl;
        _charging = d['charging'] == true;
      }
    } catch (_) {
      // best-effort; keep the last known value
    }
  }

  /// Keep a GPS fix fresh even if the position stream silently stalls (a common
  /// Wear OS failure): if the last fix is missing/stale, kick a one-shot fused
  /// fetch so lat/lon (and therefore the TB "zone") keep updating every tick.
  void _ensureGpsFresh() {
    final last = _lastPosAt;
    final stale = last == null ||
        DateTime.now().difference(last) > const Duration(seconds: 15);
    if (!stale) return;
    Geolocator.getCurrentPosition(
      locationSettings: const LocationSettings(
        accuracy: LocationAccuracy.high,
        timeLimit: Duration(seconds: 4),
      ),
    ).then((p) {
      _latestPosition = p;
      _lastPosAt = DateTime.now();
    }).catchError((_) {});
  }

  String? _lastGuardianCmd;
  String? _lastCallTs;

  /// Read TB shared attributes; if a guardian sent "dispatch", show the
  /// "guardian en route" screen and write back ack_seen so the rule chain /
  /// dashboard learns the wearer received it.
  Future<void> _pollGuardianCommand() async {
    if (_tbToken.isEmpty) return;
    try {
      final base = Uri.parse(_evaluateUrl);
      final uri = base.replace(
        port: 18080,
        pathSegments: ['api', 'v1', _tbToken, 'attributes'],
        queryParameters: {'sharedKeys': 'guardian_cmd,guardian_ack,guardian_cmd_ts'},
      );
      final r = await http.get(uri).timeout(const Duration(seconds: 4));
      if (r.statusCode != 200) return;
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      final shared = (j['shared'] as Map?) ?? const {};
      final cmd = shared['guardian_cmd'] as String?;
      final ackBy = shared['guardian_ack'] as String?;
      final ts = shared['guardian_cmd_ts']?.toString();
      if (cmd == 'call' && ts != _lastCallTs) {
        // Guardian is ringing this watch from the dashboard/phone.
        _lastCallTs = ts;
        _lastGuardianCmd = cmd;
        _enterCalled(ackBy);
      } else if (cmd == 'dispatch' && cmd != _lastGuardianCmd) {
        _lastGuardianCmd = cmd;
        _enterAcked(ackBy);
      } else if (cmd == null) {
        _lastGuardianCmd = null;
        _lastCallTs = null;
      }
    } catch (_) {
      // best-effort downlink
    }
  }

  Uri get _tbUri {
    final base = Uri.parse(_evaluateUrl);
    return base.replace(
      port: 18080,
      pathSegments: ['api', 'v1', _tbToken, 'telemetry'],
    );
  }

  /// Post HR + GPS (+ extra fields like a situation result) straight to TB.
  /// Returns true on a 2xx. This is the wearer's only uplink to the cloud now.
  Future<bool> _postTb(Map<String, dynamic> extra) async {
    if (_tbToken.isEmpty) return false;
    final pos = _latestPosition;
    try {
      final r = await http
          .post(
            _tbUri,
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'worker_id': _workerId,
              if (pos != null) ...{
                'lat': pos.latitude,
                'lon': pos.longitude,
                'accuracy_m': pos.accuracy,
              },
              if (_lastBgBpm != null) 'bpm': _lastBgBpm,
              if (_batteryPct != null) 'battery': _batteryPct,
              if (_charging != null) 'charging': _charging,
              'worn': _worn,
              ...extra,
            }),
          )
          .timeout(const Duration(seconds: 4));
      return r.statusCode >= 200 && r.statusCode < 300;
    } catch (_) {
      return false;
    }
  }

  /// Guardian acknowledged + is coming. Show a reassuring screen and tell TB
  /// (ack_seen) so the rule chain sets rescue_state + the dashboard updates.
  void _enterAcked(String? ackBy) {
    _alertCountdownTimer?.cancel();
    setState(() {
      _stage = WearStage.acked;
      _msg = ackBy != null ? '守護者前往中' : '救援前往中';
    });
    Vibration.hasVibrator().then((has) {
      if (has == true) Vibration.vibrate(pattern: [0, 200, 100, 200]);
    });
    _postTb({'ack_seen': true, 'ack_seen_at': DateTime.now().millisecondsSinceEpoch});
    Future.delayed(const Duration(seconds: 10), () {
      if (mounted && _stage == WearStage.acked) _resetToMonitoring();
    });
  }

  /// Guardian is ringing this watch from the dashboard ("call"). Show a loud
  /// "守護者正在呼叫你" screen + strong ring vibration and tell TB (call_seen).
  void _enterCalled(String? by) {
    _alertCountdownTimer?.cancel();
    setState(() {
      _stage = WearStage.called;
      _msg = by != null ? '$by 正在呼叫你' : '守護者正在呼叫你';
    });
    Vibration.hasVibrator().then((has) {
      if (has == true) {
        Vibration.vibrate(pattern: [0, 500, 200, 500, 200, 500, 200, 800]); // ring
      }
    });
    _postTb({'call_seen': true, 'call_seen_at': DateTime.now().millisecondsSinceEpoch});
    Future.delayed(const Duration(seconds: 12), () {
      if (mounted && _stage == WearStage.called) _resetToMonitoring();
    });
  }

  Future<void> _loadPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _evaluateUrl = prefs.getString('evaluateUrl') ?? _defaultEvaluateUrl;
      _workerId = prefs.getString('workerId') ?? _workerId;
      _tbToken = prefs.getString('tbToken') ?? _defaultTbToken;
    });
  }

  Future<void> _savePrefs() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('evaluateUrl', _evaluateUrl);
    await prefs.setString('workerId', _workerId);
    await prefs.setString('tbToken', _tbToken);
  }

  /// Inference endpoint. Accepts the configured URL whether it ends in
  /// /api/infer or the legacy /api/evaluate_lift; both work on the server.
  Uri get _inferUri => Uri.parse(_evaluateUrl);

  /// Tap is passive on the monitoring home screen (no manual "check"). It only
  /// means something while responding to an alert / a guardian downlink.
  Future<void> _onTap() async {
    switch (_stage) {
      case WearStage.alerting:
        _cancelAlert(); // "I'm OK"
        break;
      case WearStage.sosSent:
        _resetToMonitoring();
        break;
      case WearStage.called:
        // Acknowledge the guardian's ring, then go back to monitoring.
        _postTb({'call_answered': true});
        _resetToMonitoring();
        break;
      default:
        break; // monitoring / acked / error: passive
    }
  }

  /// Free-fall -> impact detected by the always-on sample loop. Run an immediate
  /// inference on the current window instead of waiting for the 5 s tick.
  void _onFallDetected() {
    if (_stage != WearStage.monitoring) return; // already escalating
    if (_inferInFlight) return; // the in-flight window already covers the impact
    _runInference(InferTrigger.fall);
  }

  /// Return to the always-on monitoring home. Keep the last prediction so the
  /// status ring isn't blank until the next tick.
  void _resetToMonitoring() {
    _alertCountdownTimer?.cancel();
    if (!mounted) return;
    setState(() {
      _stage = WearStage.monitoring;
      _msg = '';
    });
  }

  /// Local escalation hint from features (mirrors the TB rule-chain JS, used
  /// only to drive the watch UI — TB computes the authoritative escalation).
  String _localEscalation(String situation, Map feats) {
    double f(String k) => (feats[k] as num?)?.toDouble() ?? 0.0;
    if (situation == 'FALL') {
      final crashed = f('hr_above_baseline') > 40 ||
          f('hr_max') > 150 ||
          ((feats['post_impact_stillness'] as num?)?.toDouble() ?? 1.0) < 0.05;
      return crashed ? 'SOS_COLLAPSE' : 'ASK_OK';
    }
    return 'NONE';
  }

  /// Run one inference on a snapshot of the rolling window and route the result.
  /// Single-flight (guarded by _inferInFlight) so the periodic tick and the
  /// fall trigger can never have two /api/infer calls outstanding at once.
  Future<void> _runInference(InferTrigger trigger) async {
    if (_inferInFlight) return;
    _inferInFlight = true;
    // Snapshot before any await: the 20 ms sample timer keeps mutating the
    // live buffers during the HTTP round-trip.
    final samples = List<Map<String, dynamic>>.from(_imuBuffer);
    final hrSamples = List<Map<String, dynamic>>.from(_hrBuffer);
    if (samples.length < _minInferSamples) {
      _inferInFlight = false;
      return;
    }
    try {
      // Step 1: AI inference only (server returns situation + features; no
      // escalation, no GPS, no alarms — those belong to ThingsBoard now).
      final resp = await http
          .post(
            _inferUri,
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'worker_id': _workerId,
              'sample_rate_hz': _imuHz.toDouble(),
              'samples': samples,
              'hr_samples': hrSamples,
            }),
          )
          .timeout(const Duration(seconds: 10));
      if (resp.statusCode != 200) {
        throw Exception('HTTP ${resp.statusCode}: ${resp.body}');
      }
      final j = jsonDecode(resp.body) as Map<String, dynamic>;
      final situation = (j['situation'] as String?) ?? 'NORMAL';
      final conf = (j['situation_confidence'] as num?)?.toDouble() ?? 0;
      final feats = (j['features'] as Map?) ?? const {};
      final escalation = _localEscalation(situation, feats); // UI hint only

      // Step 2: post the situation + features + GPS straight to ThingsBoard.
      // The GuardianRules rule chain computes the real escalation, raises /
      // clears alarms, runs the delayed upgrade, etc. This replaces the
      // active-only heartbeat, so keep the cloud-status chip green too.
      final ok = await _postTb({
        'situation': situation,
        'situation_confidence': conf,
        'hr_mean': feats['hr_mean'],
        'hr_max': feats['hr_max'],
        'hr_delta_max': feats['hr_delta_max'],
        'hr_above_baseline': feats['hr_above_baseline'],
        'post_impact_stillness': feats['post_impact_stillness'],
        'active': true,
      });
      if (!mounted) return;
      setState(() {
        _situation = situation;
        _situationConf = conf;
        if (ok) {
          _lastHeartbeatPostAt = DateTime.now();
          _lastHeartbeatError = null;
        } else {
          _lastHeartbeatError = _tbToken.isEmpty ? 'no_tb' : 'tb_err';
        }
      });

      // Capture stage AFTER the awaits: the user may have tapped "I'm OK"
      // (-> monitoring) or an alert may have started while we were in flight.
      final wasMonitoring = _stage == WearStage.monitoring;
      switch (escalation) {
        case 'SOS_COLLAPSE':
          if (wasMonitoring) {
            await _hapticFeedback(escalation);
            _enterSosSent(autoDispatched: true);
          }
          break;
        case 'ASK_OK':
          if (wasMonitoring) {
            await _hapticFeedback(escalation);
            _enterAlerting();
          }
          break;
        default:
          // NONE: the monitoring status ring IS the result — no card, no reset.
          break;
      }
    } on TimeoutException {
      if (mounted) setState(() => _lastHeartbeatError = 'timeout');
    } catch (e) {
      if (!mounted) return;
      final msg = e.toString();
      final tag = (msg.contains('Failed host lookup') ||
              msg.contains('Connection refused') ||
              msg.contains('SocketException'))
          ? 'net'
          : 'err';
      setState(() => _lastHeartbeatError = tag);
    } finally {
      _inferInFlight = false;
    }
  }

  Future<void> _hapticFeedback(String escalation) async {
    final hasVib = await Vibration.hasVibrator();
    if (!hasVib) return;
    switch (escalation) {
      case 'SOS_COLLAPSE':
        Vibration.vibrate(pattern: [0, 400, 120, 400, 120, 600]); // 三段強震
        break;
      case 'ASK_OK':
        Vibration.vibrate(duration: 500); // 一長震：請回應
        break;
    }
  }

  // ---------- SOS countdown lifecycle ----------

  void _enterAlerting() {
    _alertCountdownTimer?.cancel();
    setState(() {
      _stage = WearStage.alerting;
      _alertCountdown = _alertCountdownSeconds;
      _msg = 'Are you OK?';
    });
    _alertCountdownTimer = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) {
        t.cancel();
        return;
      }
      setState(() => _alertCountdown -= 1);
      if (_alertCountdown <= 0) {
        t.cancel();
        _fireSos();
      } else {
        Vibration.vibrate(duration: 180); // per-second tick
      }
    });
  }

  void _cancelAlert() {
    _alertCountdownTimer?.cancel();
    // "I'm OK": tell TB the wearer recovered so the rule chain clears the
    // FallSuspected alarm and cancels the delayed upgrade.
    _postTb({'situation': 'NORMAL', 'user_ok': true, 'active': true});
    _resetToMonitoring();
  }

  Future<void> _fireSos() async {
    _alertCountdownTimer?.cancel();
    setState(() {
      _stage = WearStage.sosSent;
      _msg = 'SOS SENT';
    });
    final hasVib = await Vibration.hasVibrator();
    if (hasVib) Vibration.vibrate(pattern: [0, 400, 120, 400, 120, 600]);
    // No response: assert the unresolved collapse to TB. The rule chain raises
    // MedicalCollapse (and would also auto-upgrade after its delay window).
    await _postTb({
      'situation': 'FALL',
      'hr_above_baseline': 50, // force SOS_COLLAPSE in the TB escalation JS
      'no_response': true,
      'active': true,
    });
    await Future.delayed(const Duration(seconds: 8));
    if (!mounted) return;
    if (_stage == WearStage.sosSent) _resetToMonitoring();
  }

  void _enterSosSent({bool autoDispatched = false}) {
    // SOS_COLLAPSE was already broadcast server-side; just show + auto-reset.
    _alertCountdownTimer?.cancel();
    setState(() {
      _stage = WearStage.sosSent;
      _msg = 'SOS SENT';
    });
    Future.delayed(const Duration(seconds: 8), () {
      if (mounted && _stage == WearStage.sosSent) _resetToMonitoring();
    });
  }

  static const Color _accent = Color(0xFF00bcd4);
  static const Color _amber = Color(0xFFfbc02d);
  static const Color _orange = Color(0xFFf57c00);
  static const Color _danger = Color(0xFFd32f2f);
  static const Color _success = Color(0xFF388e3c);

  Color _hrColor(double? bpm) {
    if (bpm == null) return Colors.white54;
    if (bpm >= 150) return _danger;
    if (bpm >= 130) return _orange;
    if (bpm >= 100) return _amber;
    return Colors.white;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Stack(
          alignment: Alignment.center,
          children: [
            Center(child: _centerInteractive()),
            Positioned(top: 0, left: 0, right: 0, child: _topChipRow()),
            Positioned(bottom: 0, left: 0, right: 0, child: _bottomChipRow()),
          ],
        ),
      ),
    );
  }

  // --- Top chip row: [PRD/DATA] worker  ⚙ ---

  Widget _topChipRow() {
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            decoration: BoxDecoration(
              color: _accent.withValues(alpha: 0.18),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _accent, width: 1),
            ),
            child: const Text(
              'GUARD',
              style: TextStyle(
                color: _accent,
                fontSize: 9,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.5,
              ),
            ),
          ),
          const SizedBox(width: 6),
          Text(
            _workerId.length > 7 ? _workerId.substring(0, 7) : _workerId,
            style: const TextStyle(color: Colors.white70, fontSize: 11),
          ),
          const SizedBox(width: 6),
          GestureDetector(
            onTap: _openSettings,
            child: const Icon(Icons.settings, size: 16, color: Colors.white54),
          ),
        ],
      ),
    );
  }

  // --- Bottom chip row: ♥ bpm  📡 streaming status ---

  Widget _bottomChipRow() {
    final now = DateTime.now();
    final dt = _lastHeartbeatPostAt == null
        ? null
        : now.difference(_lastHeartbeatPostAt!).inSeconds;

    IconData statusIcon;
    Color statusColor;
    String statusLabel;
    final err = _lastHeartbeatError;
    if (err != null && (dt == null || dt > 6)) {
      statusIcon = Icons.cloud_off;
      statusColor = _danger;
      statusLabel = switch (err) {
        'no_gps' => 'gps',
        'timeout' => 'slow',
        'net' => 'net',
        _ => err.startsWith('http_') ? err.substring(5) : 'err',
      };
    } else if (dt == null) {
      statusIcon = Icons.more_horiz;
      statusColor = _amber;
      statusLabel = '…';
    } else if (dt > 15) {
      statusIcon = Icons.cloud_off;
      statusColor = _danger;
      statusLabel = '${dt}s';
    } else if (dt > 6) {
      statusIcon = Icons.more_horiz;
      statusColor = _amber;
      statusLabel = '${dt}s';
    } else {
      statusIcon = Icons.check_circle;
      statusColor = _success;
      statusLabel = '${dt}s';
    }

    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(Icons.favorite, size: 13, color: _hrColor(_lastBgBpm)),
              const SizedBox(width: 3),
              Text(
                _lastBgBpm == null ? '—' : _lastBgBpm!.round().toString(),
                style: TextStyle(
                  color: _hrColor(_lastBgBpm),
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(width: 2),
              const Text('bpm',
                  style: TextStyle(color: Colors.white54, fontSize: 9)),
              const SizedBox(width: 10),
              Icon(Icons.sensors, size: 12, color: statusColor),
              const SizedBox(width: 3),
              Text(statusLabel,
                  style: TextStyle(color: statusColor, fontSize: 10)),
              const SizedBox(width: 2),
              Icon(statusIcon, size: 11, color: statusColor),
            ],
          ),
          if (_inferInFlight)
            const Text(
              'analyzing…',
              style: TextStyle(color: Colors.white38, fontSize: 8),
            ),
        ],
      ),
    );
  }

  // --- Central interactive area, content varies by stage ---

  Widget _centerInteractive() {
    final size = 132.0;
    Widget child;
    switch (_stage) {
      case WearStage.monitoring:
        child = _monitoringCenter(size);
        break;
      case WearStage.error:
        child = _errorCenter(size);
        break;
      case WearStage.alerting:
        child = _alertingCenter(size);
        break;
      case WearStage.sosSent:
        child = _sosSentCenter(size);
        break;
      case WearStage.acked:
        child = _ackedCenter(size);
        break;
      case WearStage.called:
        child = _callingCenter(size);
        break;
    }

    return GestureDetector(
      onTap: _onTap,
      onLongPress: _openSettings,
      behavior: HitTestBehavior.opaque,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(width: size, height: size, child: child),
        ],
      ),
    );
  }

  /// The always-on home: a status ring that DIRECTLY shows the latest realtime
  /// prediction. Calm philosophy — the model is binary now: NORMAL shows a
  /// single steady "安全 / ACTIVE"; only FALL surfaces as a colored alert.
  Widget _monitoringCenter(double size) {
    final situation = _situation;
    Color ringColor;
    IconData icon;
    String label;
    String sub;
    if (situation == null) {
      ringColor = _accent;
      icon = Icons.sensors;
      label = '監測中';
      sub = 'monitoring';
    } else if (situation == 'FALL') {
      ringColor = _danger;
      icon = Icons.warning_amber_rounded;
      label = 'FALL';
      sub = '偵測跌倒';
    } else {
      // NORMAL -> one calm "safe / active" state.
      ringColor = _success;
      icon = Icons.check_circle;
      label = '安全';
      sub = 'ACTIVE';
    }
    return Stack(
      alignment: Alignment.center,
      children: [
        SizedBox(
          width: size,
          height: size,
          child: CircularProgressIndicator(
            value: 1.0,
            strokeWidth: 6,
            backgroundColor: Colors.white10,
            valueColor:
                AlwaysStoppedAnimation<Color>(ringColor.withValues(alpha: 0.9)),
          ),
        ),
        Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, color: ringColor, size: 26),
            const SizedBox(height: 2),
            Text(
              label,
              style: TextStyle(
                color: ringColor,
                fontSize: 20,
                fontWeight: FontWeight.w900,
                letterSpacing: 1.0,
              ),
            ),
            Text(
              sub,
              style: const TextStyle(color: Colors.white38, fontSize: 9),
            ),
            if (situation != null) ...[
              const SizedBox(height: 4),
              _confidenceBar('', _situationConf, ringColor),
            ],
          ],
        ),
      ],
    );
  }

  Widget _alertingCenter(double size) {
    final ratio = (_alertCountdown / _alertCountdownSeconds).clamp(0.0, 1.0);
    return Stack(
      alignment: Alignment.center,
      children: [
        SizedBox(
          width: size,
          height: size,
          child: CircularProgressIndicator(
            value: ratio,
            strokeWidth: 7,
            backgroundColor: Colors.white12,
            valueColor: const AlwaysStoppedAnimation<Color>(_danger),
          ),
        ),
        Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('Are you OK?',
                style: TextStyle(
                    color: Colors.white, fontSize: 12, fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text('$_alertCountdown',
                style: const TextStyle(
                    color: _danger,
                    fontSize: 40,
                    fontWeight: FontWeight.w900,
                    height: 1.0)),
            const Text("tap = I'm OK",
                style: TextStyle(color: Colors.white70, fontSize: 10)),
          ],
        ),
      ],
    );
  }

  Widget _sosSentCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: _danger.withValues(alpha: 0.25),
          border: Border.all(color: _danger, width: 3),
        ),
        child: const Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.sos, color: _danger, size: 40),
            SizedBox(height: 4),
            Text('SOS SENT',
                style: TextStyle(
                    color: Colors.white,
                    fontSize: 14,
                    fontWeight: FontWeight.w900,
                    letterSpacing: 1.0)),
            SizedBox(height: 2),
            Text('help notified',
                style: TextStyle(color: Colors.white70, fontSize: 9)),
          ],
        ),
      );

  // Guardian acknowledged + is on the way (from a TB shared-attribute downlink).
  Widget _ackedCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: _success.withValues(alpha: 0.22),
          border: Border.all(color: _success, width: 3),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.directions_run, color: _success, size: 38),
            const SizedBox(height: 4),
            Text(_msg,
                textAlign: TextAlign.center,
                style: const TextStyle(
                    color: Colors.white,
                    fontSize: 13,
                    fontWeight: FontWeight.w800)),
            const SizedBox(height: 2),
            const Text('守護者已確認',
                style: TextStyle(color: Colors.white70, fontSize: 9)),
          ],
        ),
      );

  // Guardian is ringing this watch ("call" downlink from the dashboard).
  Widget _callingCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: const Color(0xFF0891b2).withValues(alpha: 0.25),
          border: Border.all(color: const Color(0xFF38d3ff), width: 3),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.phone_in_talk, color: Color(0xFF38d3ff), size: 38),
            const SizedBox(height: 4),
            Text(_msg,
                textAlign: TextAlign.center,
                style: const TextStyle(
                    color: Colors.white,
                    fontSize: 13,
                    fontWeight: FontWeight.w800)),
            const SizedBox(height: 2),
            const Text('點一下回應',
                style: TextStyle(color: Colors.white70, fontSize: 9)),
          ],
        ),
      );

  Widget _confidenceBar(String tag, double? value, Color color) {
    final v = (value ?? 0).clamp(0.0, 1.0);
    return SizedBox(
      width: 88,
      child: Row(
        children: [
          SizedBox(
            width: 10,
            child: Text(
              tag,
              style: const TextStyle(color: Colors.white70, fontSize: 9),
            ),
          ),
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: LinearProgressIndicator(
                value: v,
                minHeight: 5,
                backgroundColor: Colors.white12,
                valueColor: AlwaysStoppedAnimation<Color>(color),
              ),
            ),
          ),
          const SizedBox(width: 4),
          SizedBox(
            width: 22,
            child: Text(
              '${(v * 100).round()}%',
              textAlign: TextAlign.right,
              style: const TextStyle(color: Colors.white70, fontSize: 9),
            ),
          ),
        ],
      ),
    );
  }

  Widget _errorCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: _danger.withValues(alpha: 0.18),
          border: Border.all(color: _danger, width: 2),
        ),
        padding: const EdgeInsets.all(10),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.error, color: _danger, size: 28),
            const SizedBox(height: 4),
            Text(
              _msg.replaceFirst('Error: ', ''),
              textAlign: TextAlign.center,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Colors.white, fontSize: 9),
            ),
          ],
        ),
      );

  void _openSettings() async {
    final controllerUrl = TextEditingController(text: _evaluateUrl);
    final controllerWorker = TextEditingController(text: _workerId);
    final controllerTb = TextEditingController(text: _tbToken);
    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF111111),
        title: const Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Settings',
                style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700)),
            Text('MN URL · Worker · ThingsBoard',
                style: TextStyle(fontSize: 9, color: Colors.white54)),
          ],
        ),
        contentPadding: const EdgeInsets.fromLTRB(14, 8, 14, 4),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _input('MN URL', controllerUrl),
              _input('Worker', controllerWorker),
              _input('TB Token', controllerTb),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.pop(ctx, false);
              _showSensorTest();
            },
            child: const Text('Sensors',
                style: TextStyle(fontSize: 12, color: Colors.white70)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel', style: TextStyle(fontSize: 12)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Save',
                style: TextStyle(
                    fontSize: 12, color: _accent, fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
    if (saved == true) {
      setState(() {
        _evaluateUrl = controllerUrl.text.trim();
        _workerId = controllerWorker.text.trim();
        _tbToken = controllerTb.text.trim();
      });
      await _savePrefs();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          backgroundColor: _success.withValues(alpha: 0.85),
          duration: const Duration(milliseconds: 1500),
          content: const Text('Saved', style: TextStyle(fontSize: 11)),
        ),
      );
    }
  }

  // On-watch live sensor test: shows the live HR value + permission status so
  // you can verify on the real device what is being received from Health Services.
  void _showSensorTest() {
    Timer? poll;
    Map<String, dynamic> diag = {};
    showDialog<void>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) {
          poll ??= Timer.periodic(const Duration(milliseconds: 1200), (_) async {
            try {
              final d = await _hrChannel.invokeMethod('heartDiagnostics');
              if (d is Map) diag = Map<String, dynamic>.from(d);
            } catch (_) {}
            if (ctx.mounted) setLocal(() {});
          });
          String fmt(double? v, String unit) =>
              v == null ? '—' : '${v.toStringAsFixed(unit == '%' ? 0 : 1)}$unit';
          Color ok(bool b) => b ? _success : _danger;
          Widget row(String k, String v, {Color? c}) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(k, style: const TextStyle(color: Colors.white70, fontSize: 11)),
                    Text(v,
                        style: TextStyle(
                            color: c ?? Colors.white,
                            fontSize: 12,
                            fontWeight: FontWeight.w700)),
                  ],
                ),
              );
          final hrPerm = diag['hasHeartPermission'] == true;
          return AlertDialog(
            backgroundColor: const Color(0xFF111111),
            title: const Text('Sensor test',
                style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700)),
            contentPadding: const EdgeInsets.fromLTRB(14, 8, 14, 4),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  row('HR', fmt(_lastBgBpm, ' bpm'),
                      c: _lastBgBpm != null ? _success : Colors.white54),
                  const Divider(color: Colors.white24),
                  row('HR perm', hrPerm ? 'granted' : 'no', c: ok(hrPerm)),
                  row('HS client',
                      diag['hasHealthServicesClient'] == true ? 'yes' : 'no'),
                  row('HR signals', '${diag['heartSignalCount'] ?? 0}'),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () =>
                    _hrChannel.invokeMethod('requestHeartRatePermission'),
                child: const Text('Grant',
                    style: TextStyle(fontSize: 12, color: _accent)),
              ),
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text('Close', style: TextStyle(fontSize: 12)),
              ),
            ],
          );
        },
      ),
    ).then((_) => poll?.cancel());
  }

  Widget _input(String label, TextEditingController c) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: TextField(
          controller: c,
          style: const TextStyle(fontSize: 13),
          decoration: InputDecoration(
            labelText: label,
            labelStyle: const TextStyle(fontSize: 11),
            isDense: true,
            contentPadding:
                const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
            border: const OutlineInputBorder(),
          ),
        ),
      );

  @override
  void dispose() {
    _sampleTimer?.cancel();
    _inferTimer?.cancel();
    _alertCountdownTimer?.cancel();
    _accSub?.cancel();
    _gyroSub?.cancel();
    _positionSub?.cancel();
    _hrChannel.invokeMethod('stopHeartRate');
    super.dispose();
  }
}
