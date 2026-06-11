import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:sensors_plus/sensors_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:vibration/vibration.dart';

void main() {
  runApp(const WearTrainerApp());
}

class WearTrainerApp extends StatelessWidget {
  const WearTrainerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Guardian Trainer',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(
        useMaterial3: true,
      ).copyWith(scaffoldBackgroundColor: Colors.black),
      home: const TrainerScreen(),
    );
  }
}

enum TrainerStage { idle, recording, uploading, saved, error }

/// One trainable situation label. `key` matches the server taxonomy in
/// posture-api/app/schemas.py (SITUATIONS) and `statsKey` matches the
/// LabelStats field names returned by GET /api/training/stats.
class TrainLabel {
  const TrainLabel(this.key, this.statsKey, this.zh, this.icon, this.color);
  final String key; // NORMAL / FALL
  final String statsKey; // normal / fall
  final String zh;
  final IconData icon;
  final Color color;
}

const List<TrainLabel> kLabels = [
  TrainLabel('NORMAL', 'normal', '正常·走動', Icons.directions_walk, Color(0xFF388e3c)),
  TrainLabel('FALL', 'fall', '跌倒', Icons.report, Color(0xFFd32f2f)),
];

class TrainerScreen extends StatefulWidget {
  const TrainerScreen({super.key});
  @override
  State<TrainerScreen> createState() => _TrainerScreenState();
}

class _TrainerScreenState extends State<TrainerScreen> {
  // Base URL of the posture-api edge/MN server. Training endpoints are derived
  // from it: POST {base}/api/training and GET {base}/api/training/stats.
  static const String _defaultServerBase = 'http://140.113.123.43:8000';
  static const MethodChannel _hrChannel = MethodChannel('wear_os/heart_rate');
  // Fixed labelled-capture window. ~5 s at 50 Hz ≈ 250 IMU samples — plenty for
  // the 27-D feature extractor on the server side.
  static const Duration _window = Duration(seconds: 5);
  static const Duration _samplePeriod = Duration(milliseconds: 20); // 50 Hz

  TrainerStage _stage = TrainerStage.idle;
  String _msg = '';
  String _serverBase = _defaultServerBase;
  String _workerId = 'W-001';
  int _labelIndex = 0;

  final List<Map<String, dynamic>> _imuBuffer = [];
  final List<Map<String, dynamic>> _hrBuffer = [];
  Map<String, double> _lastAcc = {'x': 0, 'y': 0, 'z': 1};
  Map<String, double> _lastGyro = {'x': 0, 'y': 0, 'z': 0};

  StreamSubscription? _accSub;
  StreamSubscription? _gyroSub;
  Timer? _sampleTimer;
  Timer? _windowTimer;
  Timer? _tickTimer;
  double? _recordingStart;
  double _elapsedS = 0;
  double? _lastBpm;

  // Per-label saved-record counts from GET /api/training/stats.
  Map<String, int> _stats = {};
  int _totalRecords = 0;
  bool _statsLoading = false;
  String? _statsError;

  TrainLabel get _label => kLabels[_labelIndex];

  @override
  void initState() {
    super.initState();
    _hrChannel.setMethodCallHandler(_onHrCall);
    _loadPrefs().then((_) {
      _startHeartRate();
      _fetchStats();
    });
  }

  Future<dynamic> _onHrCall(MethodCall call) async {
    if (call.method != 'onHeartRate') return;
    final args = call.arguments;
    double? bpm;
    if (args is Map) {
      bpm = (args['bpm'] as num?)?.toDouble();
      if (_stage == TrainerStage.recording && bpm != null) {
        _hrBuffer.add({
          'timestamp': args['timestamp'],
          'bpm': bpm,
          'accuracy': args['accuracy'],
        });
      }
    } else if (args is num) {
      bpm = args.toDouble();
    }
    if (bpm != null && mounted) setState(() => _lastBpm = bpm);
  }

  Future<void> _startHeartRate() async {
    try {
      await _hrChannel.invokeMethod('requestHeartRatePermission');
      await _hrChannel.invokeMethod('startHeartRate');
    } catch (e) {
      // best-effort; the watch may not expose Health Services in the emulator
      _log('heart-rate unavailable: $e');
    }
  }

  Future<void> _loadPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _serverBase = prefs.getString('trainerServerBase') ?? _defaultServerBase;
      _workerId = prefs.getString('trainerWorkerId') ?? _workerId;
      _labelIndex = prefs.getInt('trainerLabelIndex') ?? 0;
      if (_labelIndex < 0 || _labelIndex >= kLabels.length) _labelIndex = 0;
    });
  }

  Future<void> _savePrefs() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('trainerServerBase', _serverBase);
    await prefs.setString('trainerWorkerId', _workerId);
    await prefs.setInt('trainerLabelIndex', _labelIndex);
  }

  Uri _api(String path) {
    final base = _serverBase.endsWith('/')
        ? _serverBase.substring(0, _serverBase.length - 1)
        : _serverBase;
    return Uri.parse('$base$path');
  }

  // ---------- stats ----------

  Future<void> _fetchStats() async {
    setState(() {
      _statsLoading = true;
      _statsError = null;
    });
    try {
      final r = await http.get(_api('/api/training/stats')).timeout(
            const Duration(seconds: 6),
          );
      if (r.statusCode != 200) {
        throw Exception('HTTP ${r.statusCode}');
      }
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      final stats = (j['stats'] as Map?) ?? const {};
      if (!mounted) return;
      setState(() {
        // The server's LabelStats also carries non-numeric fields such as
        // `last_recorded_at` (a String). Only keep numeric counts here so a
        // String value never gets force-cast to num.
        _stats = {
          for (final e in stats.entries)
            if (e.value is num) e.key.toString(): (e.value as num).toInt(),
        };
        _totalRecords = (stats['total_records'] as num?)?.toInt() ?? 0;
        _statsLoading = false;
      });
      _log('stats ok · total=$_totalRecords · $_stats');
    } catch (e, st) {
      _log('fetchStats FAILED: $e', st);
      if (!mounted) return;
      setState(() {
        _statsLoading = false;
        _statsError = _shortErr(e);
      });
    }
  }

  // Tagged console logging so events show up in `flutter run` / `flutter logs`.
  // debugPrint is rate-limited and stripped from release builds.
  void _log(String msg, [StackTrace? st]) {
    debugPrint('[trainer] $msg');
    if (st != null) debugPrint(st.toString());
  }

  String _shortErr(Object e) {
    final s = e.toString();
    if (s.contains('Failed host lookup') ||
        s.contains('Connection refused') ||
        s.contains('SocketException')) {
      return 'net';
    }
    if (s.contains('TimeoutException')) return 'slow';
    return s.length > 24 ? '${s.substring(0, 24)}…' : s;
  }

  // ---------- recording lifecycle ----------

  void _cycleLabel() {
    if (_stage != TrainerStage.idle) return;
    setState(() => _labelIndex = (_labelIndex + 1) % kLabels.length);
    _savePrefs();
    Vibration.hasVibrator().then((h) {
      if (h == true) Vibration.vibrate(duration: 40);
    });
  }

  Future<void> _startRecording() async {
    if (_stage != TrainerStage.idle) return;
    _imuBuffer.clear();
    _hrBuffer.clear();
    final start = DateTime.now().millisecondsSinceEpoch / 1000.0;
    _recordingStart = start;
    _elapsedS = 0;

    setState(() {
      _stage = TrainerStage.recording;
      _msg = '錄製中';
    });
    Vibration.hasVibrator().then((h) {
      if (h == true) Vibration.vibrate(duration: 120);
    });

    _accSub = accelerometerEventStream(samplingPeriod: _samplePeriod).listen((e) {
      _lastAcc = {
        'x': e.x / 9.80665,
        'y': e.y / 9.80665,
        'z': e.z / 9.80665,
      };
    });
    _gyroSub = gyroscopeEventStream(samplingPeriod: _samplePeriod).listen((e) {
      _lastGyro = {'x': e.x, 'y': e.y, 'z': e.z};
    });

    _sampleTimer = Timer.periodic(_samplePeriod, (t) {
      final ts = start + t.tick * (_samplePeriod.inMilliseconds / 1000.0);
      _imuBuffer.add({
        'timestamp': ts,
        'acc_x': _lastAcc['x'],
        'acc_y': _lastAcc['y'],
        'acc_z': _lastAcc['z'],
        'gyro_x': _lastGyro['x'],
        'gyro_y': _lastGyro['y'],
        'gyro_z': _lastGyro['z'],
      });
    });

    _tickTimer = Timer.periodic(const Duration(milliseconds: 200), (_) {
      if (!mounted || _recordingStart == null) return;
      setState(() {
        _elapsedS =
            DateTime.now().millisecondsSinceEpoch / 1000.0 - _recordingStart!;
      });
    });

    _windowTimer = Timer(_window, _stopAndUpload);
  }

  Future<void> _stopAndUpload() async {
    if (_stage != TrainerStage.recording) return;
    _sampleTimer?.cancel();
    _windowTimer?.cancel();
    _tickTimer?.cancel();
    await _accSub?.cancel();
    await _gyroSub?.cancel();

    setState(() {
      _stage = TrainerStage.uploading;
      _msg = '上傳中';
    });
    Vibration.hasVibrator().then((h) {
      if (h == true) Vibration.vibrate(duration: 200);
    });

    if (_imuBuffer.length < 16) {
      setState(() {
        _stage = TrainerStage.error;
        _msg = '樣本太少 (${_imuBuffer.length})';
      });
      _autoResetAfter(const Duration(seconds: 3));
      return;
    }

    try {
      await _uploadRecord();
    } catch (e, st) {
      _log('upload FAILED: $e', st);
      if (!mounted) return;
      setState(() {
        _stage = TrainerStage.error;
        _msg = _shortErr(e);
      });
      _autoResetAfter(const Duration(seconds: 3));
    }
  }

  Future<void> _uploadRecord() async {
    final body = jsonEncode({
      'records': [
        {
          'situation': _label.key,
          'source': 'wear-os-trainer',
          'worker_id': _workerId,
          'samples': _imuBuffer,
          'hr_samples': _hrBuffer,
        },
      ],
    });
    _log('upload → ${_api('/api/training')} · ${_label.key} · '
        'imu=${_imuBuffer.length} hr=${_hrBuffer.length}');
    final r = await http
        .post(
          _api('/api/training'),
          headers: {'Content-Type': 'application/json'},
          body: body,
        )
        .timeout(const Duration(seconds: 12));
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw Exception('HTTP ${r.statusCode}: ${r.body}');
    }
    _log('upload ok · HTTP ${r.statusCode}');
    // Optimistic local bump so the count updates instantly, then refresh.
    if (!mounted) return;
    setState(() {
      _stats[_label.statsKey] = (_stats[_label.statsKey] ?? 0) + 1;
      _totalRecords += 1;
      _stage = TrainerStage.saved;
      _msg = '已儲存';
    });
    Vibration.hasVibrator().then((h) {
      if (h == true) Vibration.vibrate(pattern: [0, 120, 80, 120]);
    });
    _fetchStats();
    _autoResetAfter(const Duration(seconds: 2));
  }

  void _autoResetAfter(Duration d) {
    Future.delayed(d, () {
      if (!mounted) return;
      if (_stage == TrainerStage.saved || _stage == TrainerStage.error) {
        setState(() {
          _stage = TrainerStage.idle;
          _msg = '';
        });
      }
    });
  }

  void _onCenterTap() {
    switch (_stage) {
      case TrainerStage.idle:
        _startRecording();
        break;
      case TrainerStage.recording:
        _stopAndUpload(); // allow early stop
        break;
      case TrainerStage.saved:
      case TrainerStage.error:
        setState(() {
          _stage = TrainerStage.idle;
          _msg = '';
        });
        break;
      case TrainerStage.uploading:
        break;
    }
  }

  // ---------- UI ----------

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Stack(
          alignment: Alignment.center,
          children: [
            Center(child: _center()),
            Positioned(top: 0, left: 0, right: 0, child: _topRow()),
            Positioned(bottom: 0, left: 0, right: 0, child: _bottomRow()),
          ],
        ),
      ),
    );
  }

  Widget _topRow() {
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            decoration: BoxDecoration(
              color: const Color(0xFFf57c00).withValues(alpha: 0.18),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: const Color(0xFFf57c00), width: 1),
            ),
            child: const Text(
              'TRAIN',
              style: TextStyle(
                color: Color(0xFFffb74d),
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

  Widget _bottomRow() {
    final total = _totalRecords;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(Icons.favorite, size: 13, color: _hrColor(_lastBpm)),
              const SizedBox(width: 3),
              Text(
                _lastBpm == null ? '—' : _lastBpm!.round().toString(),
                style: TextStyle(
                  color: _hrColor(_lastBpm),
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(width: 2),
              const Text('bpm', style: TextStyle(color: Colors.white54, fontSize: 9)),
              const SizedBox(width: 10),
              Icon(
                _statsError != null
                    ? Icons.cloud_off
                    : (_statsLoading ? Icons.more_horiz : Icons.dataset),
                size: 12,
                color: _statsError != null
                    ? const Color(0xFFd32f2f)
                    : Colors.white54,
              ),
              const SizedBox(width: 3),
              Text(
                _statsError ?? '$total',
                style: TextStyle(
                  color: _statsError != null
                      ? const Color(0xFFd32f2f)
                      : Colors.white54,
                  fontSize: 10,
                ),
              ),
            ],
          ),
          if (_stage == TrainerStage.recording)
            Text(
              'imu ${_imuBuffer.length} · hr ${_hrBuffer.length}',
              style: const TextStyle(color: Colors.white54, fontSize: 8),
            ),
        ],
      ),
    );
  }

  Widget _center() {
    const size = 134.0;
    Widget child;
    switch (_stage) {
      case TrainerStage.idle:
        child = _idleCenter(size);
        break;
      case TrainerStage.recording:
        child = _recordingCenter(size);
        break;
      case TrainerStage.uploading:
        child = _uploadingCenter(size);
        break;
      case TrainerStage.saved:
        child = _savedCenter(size);
        break;
      case TrainerStage.error:
        child = _errorCenter(size);
        break;
    }
    return GestureDetector(
      onTap: _onCenterTap,
      onLongPress: _openSettings,
      behavior: HitTestBehavior.opaque,
      child: SizedBox(width: size, height: size, child: child),
    );
  }

  Widget _idleCenter(double size) {
    final lbl = _label;
    final count = _stats[lbl.statsKey] ?? 0;
    return Container(
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: lbl.color.withValues(alpha: 0.16),
        border: Border.all(color: lbl.color, width: 2),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(lbl.icon, color: lbl.color, size: 26),
          const SizedBox(height: 2),
          Text(
            lbl.zh,
            textAlign: TextAlign.center,
            style: TextStyle(
              color: lbl.color,
              fontSize: 16,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 1),
          Text(
            '已採 $count',
            style: const TextStyle(color: Colors.white70, fontSize: 10),
          ),
          const SizedBox(height: 4),
          // Tap-to-cycle label selector (separate hit target from REC).
          GestureDetector(
            onTap: _cycleLabel,
            behavior: HitTestBehavior.opaque,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
              decoration: BoxDecoration(
                color: Colors.white12,
                borderRadius: BorderRadius.circular(10),
              ),
              child: const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.swap_horiz, size: 12, color: Colors.white70),
                  SizedBox(width: 3),
                  Text('換標籤',
                      style: TextStyle(color: Colors.white70, fontSize: 10)),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _recordingCenter(double size) {
    final ratio = (_elapsedS / _window.inSeconds).clamp(0.0, 1.0);
    final remaining = (_window.inSeconds - _elapsedS).clamp(0.0, 999.0);
    final lbl = _label;
    return Stack(
      alignment: Alignment.center,
      children: [
        SizedBox(
          width: size,
          height: size,
          child: CircularProgressIndicator(
            value: ratio,
            strokeWidth: 6,
            backgroundColor: Colors.white12,
            valueColor: AlwaysStoppedAnimation<Color>(lbl.color),
          ),
        ),
        Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(lbl.zh,
                style: TextStyle(
                    color: lbl.color, fontSize: 13, fontWeight: FontWeight.w800)),
            Text(
              remaining.ceil().toString(),
              style: TextStyle(
                color: lbl.color,
                fontSize: 34,
                fontWeight: FontWeight.w900,
                height: 1.0,
              ),
            ),
            const Text('點一下提早停',
                style: TextStyle(color: Colors.white54, fontSize: 9)),
          ],
        ),
      ],
    );
  }

  Widget _uploadingCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: const Color(0xFFf57c00).withValues(alpha: 0.10),
        ),
        child: const Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            SizedBox(
              width: 34,
              height: 34,
              child: CircularProgressIndicator(
                strokeWidth: 4,
                valueColor: AlwaysStoppedAnimation<Color>(Color(0xFFf57c00)),
              ),
            ),
            SizedBox(height: 8),
            Text('上傳中', style: TextStyle(color: Colors.white, fontSize: 11)),
          ],
        ),
      );

  Widget _savedCenter(double size) {
    final lbl = _label;
    final count = _stats[lbl.statsKey] ?? 0;
    return Container(
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: const Color(0xFF388e3c).withValues(alpha: 0.20),
        border: Border.all(color: const Color(0xFF388e3c), width: 3),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.cloud_done, color: Color(0xFF66bb6a), size: 30),
          const SizedBox(height: 3),
          Text('${lbl.zh} +1',
              style: const TextStyle(
                  color: Colors.white, fontSize: 14, fontWeight: FontWeight.w900)),
          const SizedBox(height: 1),
          Text('該標籤共 $count',
              style: const TextStyle(color: Colors.white70, fontSize: 10)),
        ],
      ),
    );
  }

  Widget _errorCenter(double size) => Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: const Color(0xFFd32f2f).withValues(alpha: 0.18),
          border: Border.all(color: const Color(0xFFd32f2f), width: 2),
        ),
        padding: const EdgeInsets.all(10),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.error, color: Color(0xFFd32f2f), size: 26),
            const SizedBox(height: 4),
            Text(
              _msg,
              textAlign: TextAlign.center,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Colors.white, fontSize: 9),
            ),
          ],
        ),
      );

  Color _hrColor(double? bpm) {
    if (bpm == null) return Colors.white54;
    if (bpm >= 150) return const Color(0xFFd32f2f);
    if (bpm >= 130) return const Color(0xFFf57c00);
    if (bpm >= 100) return const Color(0xFFfbc02d);
    return Colors.white;
  }

  // ---------- settings ----------

  void _openSettings() async {
    final ctrlBase = TextEditingController(text: _serverBase);
    final ctrlWorker = TextEditingController(text: _workerId);
    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF111111),
        title: const Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Trainer 設定',
                style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700)),
            Text('Server base · Worker',
                style: TextStyle(fontSize: 9, color: Colors.white54)),
          ],
        ),
        contentPadding: const EdgeInsets.fromLTRB(14, 8, 14, 4),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _input('Server base', ctrlBase),
              _input('Worker', ctrlWorker),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.pop(ctx, false);
              _fetchStats();
            },
            child: const Text('Refresh',
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
                    fontSize: 12,
                    color: Color(0xFFf57c00),
                    fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
    if (saved == true) {
      setState(() {
        _serverBase = ctrlBase.text.trim();
        _workerId = ctrlWorker.text.trim();
      });
      await _savePrefs();
      _fetchStats();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          backgroundColor: const Color(0xFF388e3c).withValues(alpha: 0.85),
          duration: const Duration(milliseconds: 1500),
          content: const Text('Saved', style: TextStyle(fontSize: 11)),
        ),
      );
    }
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
    _windowTimer?.cancel();
    _tickTimer?.cancel();
    _accSub?.cancel();
    _gyroSub?.cancel();
    _hrChannel.invokeMethod('stopHeartRate');
    super.dispose();
  }
}
