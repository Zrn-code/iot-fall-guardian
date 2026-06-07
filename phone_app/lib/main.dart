import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:latlong2/latlong.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:vibration/vibration.dart';

void main() {
  runApp(const GuardianApp());
}

class GuardianApp extends StatelessWidget {
  const GuardianApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Guardian Console',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(useMaterial3: true).copyWith(
        scaffoldBackgroundColor: const Color(0xFF0c1320),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF06b6d4),
          brightness: Brightness.dark,
        ),
      ),
      home: const RootGate(),
    );
  }
}

// ============================================================================
// Roles: a 守護者 Guardian logs into ThingsBoard as a CUSTOMER_USER and monitors
// every 配戴者 Wearer owned by their customer. The phone is a pure TB client —
// it has no telemetry of its own (guardians have no BPM). "Nearby priority" is
// computed locally from the phone's GPS vs each wearer's GPS.
// ============================================================================

const double kNearbyRadiusM = 50.0;

class TbSession {
  String baseUrl;
  String token;
  String refreshToken;
  String email;
  String customerId;

  TbSession({
    required this.baseUrl,
    required this.token,
    required this.refreshToken,
    required this.email,
    required this.customerId,
  });

  Map<String, dynamic> toJson() => {
        'baseUrl': baseUrl,
        'token': token,
        'refreshToken': refreshToken,
        'email': email,
        'customerId': customerId,
      };

  factory TbSession.fromJson(Map<String, dynamic> j) => TbSession(
        baseUrl: j['baseUrl'] as String,
        token: j['token'] as String,
        refreshToken: j['refreshToken'] as String,
        email: j['email'] as String,
        customerId: j['customerId'] as String,
      );

  static Future<TbSession?> load() async {
    final p = await SharedPreferences.getInstance();
    final s = p.getString('tbSession');
    if (s == null) return null;
    try {
      return TbSession.fromJson(jsonDecode(s) as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('tbSession', jsonEncode(toJson()));
  }

  static Future<void> clear() async {
    final p = await SharedPreferences.getInstance();
    await p.remove('tbSession');
  }
}

class Wearer {
  final String id;
  final String name; // Wearer_W-001
  final String label; // W-001
  double? lat, lon, bpm, lastHrMax;
  String? situation, escalation;
  bool active;
  double? distanceM; // from the guardian's phone, computed locally

  Wearer({
    required this.id,
    required this.name,
    required this.label,
    this.active = false,
  });

  String get shortName =>
      label.isNotEmpty ? label : name.replaceFirst('Wearer_', '');

  bool get hasAlarm =>
      escalation == 'SOS_COLLAPSE' || situation == 'FALL';

  bool get isCritical => escalation == 'SOS_COLLAPSE';
}

class TbAlarm {
  final String id;
  final String type;
  final String severity;
  final String status;
  final String originatorName;
  final double createdTime;
  final double? hrMax;

  TbAlarm({
    required this.id,
    required this.type,
    required this.severity,
    required this.status,
    required this.originatorName,
    required this.createdTime,
    this.hrMax,
  });

  factory TbAlarm.fromJson(Map<String, dynamic> j) {
    double? hr;
    final d = j['details'];
    if (d is Map && d['hr_max'] != null) {
      hr = (d['hr_max'] as num?)?.toDouble();
    }
    return TbAlarm(
      id: (j['id'] as Map)['id'] as String,
      type: j['type'] as String? ?? '?',
      severity: j['severity'] as String? ?? 'WARNING',
      status: j['status'] as String? ?? '',
      originatorName: j['originatorName'] as String? ?? '?',
      createdTime: (j['createdTime'] as num?)?.toDouble() ?? 0,
      hrMax: hr,
    );
  }

  bool get active => status.startsWith('ACTIVE');
  int get sevRank => switch (severity) {
        'CRITICAL' => 3,
        'MAJOR' => 2,
        'WARNING' => 1,
        _ => 0,
      };
}

String prettyAlarmType(String t) => switch (t) {
      'MedicalCollapse' => '倒地·生命徵象驟變',
      'FallSuspected' => '偵測到跌倒',
      _ => t,
    };

String prettySituation(String? s) => switch (s) {
      'FALL' => '跌倒',
      'NORMAL' => '正常',
      null => '—',
      _ => s,
    };

// ============================================================================
// ThingsBoard REST client (customer-user scope)
// ============================================================================

class TbClient {
  final TbSession session;
  TbClient(this.session);

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'X-Authorization': 'Bearer ${session.token}',
      };

  static Future<TbSession> login(String base, String email, String password) async {
    base = base.trim().replaceAll(RegExp(r'/+$'), '');
    final r = await http
        .post(
          Uri.parse('$base/api/auth/login'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'username': email, 'password': password}),
        )
        .timeout(const Duration(seconds: 8));
    if (r.statusCode != 200) {
      throw Exception(_loginError(r));
    }
    final j = jsonDecode(r.body) as Map<String, dynamic>;
    final token = j['token'] as String;
    final refresh = j['refreshToken'] as String;
    // Resolve customerId + canonical email.
    final ur = await http.get(
      Uri.parse('$base/api/auth/user'),
      headers: {'X-Authorization': 'Bearer $token'},
    ).timeout(const Duration(seconds: 8));
    if (ur.statusCode != 200) throw Exception('auth/user ${ur.statusCode}');
    final u = jsonDecode(ur.body) as Map<String, dynamic>;
    final cid = (u['customerId'] as Map?)?['id'] as String? ?? '';
    return TbSession(
      baseUrl: base,
      token: token,
      refreshToken: refresh,
      email: u['email'] as String? ?? email,
      customerId: cid,
    );
  }

  static String _loginError(http.Response r) {
    try {
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      final m = j['message'] as String?;
      if (m != null) return m;
    } catch (_) {}
    return 'login failed (HTTP ${r.statusCode})';
  }

  Future<bool> _refresh() async {
    try {
      final r = await http
          .post(
            Uri.parse('${session.baseUrl}/api/auth/token'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'refreshToken': session.refreshToken}),
          )
          .timeout(const Duration(seconds: 8));
      if (r.statusCode != 200) return false;
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      session.token = j['token'] as String;
      session.refreshToken = j['refreshToken'] as String;
      await session.save();
      return true;
    } catch (_) {
      return false;
    }
  }

  Future<dynamic> _get(String path) async {
    var r = await http
        .get(Uri.parse('${session.baseUrl}$path'), headers: _headers)
        .timeout(const Duration(seconds: 6));
    if (r.statusCode == 401 && await _refresh()) {
      r = await http
          .get(Uri.parse('${session.baseUrl}$path'), headers: _headers)
          .timeout(const Duration(seconds: 6));
    }
    if (r.statusCode != 200) {
      throw Exception('GET $path -> ${r.statusCode}');
    }
    return jsonDecode(r.body);
  }

  Future<bool> _post(String path, Map<String, dynamic> body) async {
    var r = await http
        .post(Uri.parse('${session.baseUrl}$path'),
            headers: _headers, body: jsonEncode(body))
        .timeout(const Duration(seconds: 6));
    if (r.statusCode == 401 && await _refresh()) {
      r = await http
          .post(Uri.parse('${session.baseUrl}$path'),
              headers: _headers, body: jsonEncode(body))
          .timeout(const Duration(seconds: 6));
    }
    return r.statusCode >= 200 && r.statusCode < 300;
  }

  /// This guardian's own device id (`Guardian_<localpart>`), used to upload the
  /// guardian's own GPS so their position is first-class TB data.
  Future<String?> findOwnGuardianDeviceId() async {
    final localPart = session.email.split('@').first;
    final j = await _get(
        '/api/customer/${session.customerId}/devices?pageSize=200&page=0');
    final data = ((j as Map)['data'] as List).cast<Map<String, dynamic>>();
    for (final d in data) {
      if ((d['type'] as String?) == 'guardian' &&
          (d['name'] as String? ?? '').endsWith(localPart)) {
        return (d['id'] as Map)['id'] as String;
      }
    }
    return null;
  }

  /// Upload the guardian's own GPS to their guardian device (REST telemetry —
  /// a customer-user may write to assigned devices, no device token needed).
  Future<void> uploadGuardianGps(String guardianDeviceId, double lat, double lon) async {
    await _post(
      '/api/plugins/telemetry/DEVICE/$guardianDeviceId/timeseries/ANY?scope=ANY',
      {'lat': lat, 'lon': lon, 'active': true, 'entity_type': 'guardian'},
    );
  }

  /// Send a downlink command to a wearer via TB shared attributes (B4).
  /// The wearer's watch reads guardian_cmd and shows "guardian en route".
  Future<bool> sendWearerCommand(String wearerId, String cmd) async {
    return _post(
      '/api/plugins/telemetry/DEVICE/$wearerId/SHARED_SCOPE',
      {
        'guardian_cmd': cmd,
        'guardian_ack': session.email,
        'guardian_ack_ts': DateTime.now().millisecondsSinceEpoch,
      },
    );
  }

  Future<List<Wearer>> fetchWearers() async {
    final j = await _get(
        '/api/customer/${session.customerId}/devices?pageSize=200&page=0');
    final data = ((j as Map)['data'] as List).cast<Map<String, dynamic>>();
    return data
        .where((d) => (d['type'] as String?) == 'wearer')
        .map((d) => Wearer(
              id: (d['id'] as Map)['id'] as String,
              name: d['name'] as String? ?? '?',
              label: d['label'] as String? ?? '',
            ))
        .toList();
  }

  Future<void> fillLatest(Wearer w) async {
    const keys = 'lat,lon,bpm,last_situation,last_escalation,last_hr_max,active';
    final j = await _get(
        '/api/plugins/telemetry/DEVICE/${w.id}/values/timeseries?keys=$keys');
    final m = j as Map<String, dynamic>;
    double? num1(String k) {
      final v = m[k];
      if (v is List && v.isNotEmpty) return double.tryParse('${v.first['value']}');
      return null;
    }

    String? str(String k) {
      final v = m[k];
      if (v is List && v.isNotEmpty) return '${v.first['value']}';
      return null;
    }

    w.lat = num1('lat');
    w.lon = num1('lon');
    w.bpm = num1('bpm');
    w.lastHrMax = num1('last_hr_max');
    w.situation = str('last_situation');
    w.escalation = str('last_escalation');
    w.active = str('active') == 'true';
  }

  Future<List<TbAlarm>> fetchActiveAlarms() async {
    final j = await _get(
        '/api/alarms?pageSize=100&page=0&sortProperty=createdTime&sortOrder=DESC&searchStatus=ACTIVE');
    final data = ((j as Map)['data'] as List).cast<Map<String, dynamic>>();
    return data.map(TbAlarm.fromJson).toList();
  }
}

// ============================================================================
// Root gate: decide Login vs Console based on a stored session
// ============================================================================

class RootGate extends StatefulWidget {
  const RootGate({super.key});
  @override
  State<RootGate> createState() => _RootGateState();
}

class _RootGateState extends State<RootGate> {
  TbSession? _session;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    _restore();
  }

  Future<void> _restore() async {
    final s = await TbSession.load();
    if (s != null) {
      // validate the stored token; refresh if needed
      final c = TbClient(s);
      try {
        await c.fetchWearers();
        _session = s;
      } catch (_) {
        if (await c._refresh()) {
          _session = s;
        }
      }
    }
    if (mounted) setState(() => _ready = true);
  }

  void _onLogin(TbSession s) => setState(() => _session = s);
  void _onLogout() {
    TbSession.clear();
    setState(() => _session = null);
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    if (_session == null) return LoginScreen(onLogin: _onLogin);
    return ConsoleShell(session: _session!, onLogout: _onLogout);
  }
}

// ============================================================================
// Login screen — real ThingsBoard account (守護者登入)
// ============================================================================

class LoginScreen extends StatefulWidget {
  final void Function(TbSession) onLogin;
  const LoginScreen({super.key, required this.onLogin});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _base = TextEditingController(text: 'http://140.113.123.43:18080');
  final _email = TextEditingController(text: 'guardian1@guardian.local');
  final _pw = TextEditingController(text: ''); // set at runtime; never ship a real password
  bool _busy = false;
  String? _error;

  Future<void> _restoreBase() async {
    final p = await SharedPreferences.getInstance();
    final b = p.getString('lastBase');
    if (b != null && mounted) _base.text = b;
  }

  @override
  void initState() {
    super.initState();
    _restoreBase();
  }

  Future<void> _doLogin() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final s = await TbClient.login(_base.text, _email.text.trim(), _pw.text);
      await s.save();
      final p = await SharedPreferences.getInstance();
      await p.setString('lastBase', s.baseUrl);
      widget.onLogin(s);
    } catch (e) {
      setState(() => _error = e.toString().replaceFirst('Exception: ', ''));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(Icons.shield_moon, size: 56, color: Color(0xFF38d3ff)),
              const SizedBox(height: 10),
              const Text('Guardian Console',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800)),
              const Text('守護者登入 — 監看所有配戴者',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.white54, fontSize: 12)),
              const SizedBox(height: 24),
              TextField(
                controller: _base,
                decoration: const InputDecoration(
                    labelText: 'ThingsBoard URL', prefixIcon: Icon(Icons.dns)),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                decoration: const InputDecoration(
                    labelText: '帳號 Email', prefixIcon: Icon(Icons.person)),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _pw,
                obscureText: true,
                decoration: const InputDecoration(
                    labelText: '密碼 Password', prefixIcon: Icon(Icons.lock)),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                children: [
                  for (final acc in const [
                    'guardian1@guardian.local',
                    'guardian2@guardian.local'
                  ])
                    ActionChip(
                      label: Text(acc.split('@').first,
                          style: const TextStyle(fontSize: 11)),
                      onPressed: () => setState(() {
                        _email.text = acc;
                        _pw.text = 'guardian';
                      }),
                    ),
                ],
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(_error!,
                    style: const TextStyle(color: Color(0xFFff5252), fontSize: 12)),
              ],
              const SizedBox(height: 18),
              FilledButton.icon(
                onPressed: _busy ? null : _doLogin,
                icon: _busy
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.login),
                label: Text(_busy ? '登入中…' : '登入 Login'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ============================================================================
// Console shell — Wearers / Map / Alarms + global priority banner
// ============================================================================

class ConsoleShell extends StatefulWidget {
  final TbSession session;
  final VoidCallback onLogout;
  const ConsoleShell({super.key, required this.session, required this.onLogout});
  @override
  State<ConsoleShell> createState() => _ConsoleShellState();
}

class _ConsoleShellState extends State<ConsoleShell> {
  late final TbClient _tb = TbClient(widget.session);
  int _tab = 0;
  Timer? _poll;
  Timer? _gpsTimer;
  Position? _me;
  List<Wearer> _wearers = [];
  List<TbAlarm> _alarms = [];
  String? _pollError;
  OverlayEntry? _banner;
  final Set<String> _seenAlarmIds = {};
  bool _firstLoad = true;

  String? _guardianDeviceId;

  @override
  void initState() {
    super.initState();
    _resolveGuardianDevice();
    _ensureGps();
    _tick();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _tick());
  }

  Future<void> _resolveGuardianDevice() async {
    try {
      _guardianDeviceId = await _tb.findOwnGuardianDeviceId();
    } catch (_) {}
  }

  Future<void> _ensureGps() async {
    await Permission.locationWhenInUse.request();
    _readGps();
    _gpsTimer = Timer.periodic(const Duration(seconds: 5), (_) => _readGps());
  }

  Future<void> _readGps() async {
    try {
      if (!(await Permission.locationWhenInUse.status).isGranted) return;
      final pos = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
            accuracy: LocationAccuracy.high, timeLimit: Duration(seconds: 4)),
      );
      _me = pos;
      _recomputeDistances();
      // Upload the guardian's OWN GPS to TB (first-class position data).
      final gid = _guardianDeviceId;
      if (gid != null) {
        _tb.uploadGuardianGps(gid, pos.latitude, pos.longitude);
      }
      if (mounted) setState(() {});
    } catch (_) {}
  }

  Future<void> _sendCommand(Wearer w, String cmd) async {
    final ok = await _tb.sendWearerCommand(w.id, cmd);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      duration: const Duration(milliseconds: 1500),
      content: Text(ok ? '已通知 ${w.shortName}：前往中' : '通知失敗'),
    ));
  }

  void _recomputeDistances() {
    final me = _me;
    if (me == null) return;
    const d = Distance();
    for (final w in _wearers) {
      if (w.lat != null && w.lon != null) {
        w.distanceM = d.as(LengthUnit.Meter,
            LatLng(me.latitude, me.longitude), LatLng(w.lat!, w.lon!));
      }
    }
  }

  Future<void> _tick() async {
    try {
      final wearers = await _tb.fetchWearers();
      for (final w in wearers) {
        try {
          await _tb.fillLatest(w);
        } catch (_) {}
      }
      final alarms = await _tb.fetchActiveAlarms();
      _wearers = wearers;
      _alarms = alarms;
      _pollError = null;
      _recomputeDistances();
      _detectNewAlarms(alarms);
      if (mounted) setState(() {});
    } catch (e) {
      if (mounted) {
        setState(() => _pollError = e.toString().replaceFirst('Exception: ', ''));
      }
    }
  }

  void _detectNewAlarms(List<TbAlarm> alarms) {
    // Nearby-first then severity so the most urgent drives the banner.
    final fresh = alarms.where((a) => !_seenAlarmIds.contains(a.id)).toList();
    for (final a in alarms) {
      _seenAlarmIds.add(a.id);
    }
    if (_firstLoad) {
      _firstLoad = false;
      return; // don't flood on first poll
    }
    if (fresh.isEmpty) return;
    fresh.sort((a, b) {
      final na = _isNearbyAlarm(a) ? 1 : 0;
      final nb = _isNearbyAlarm(b) ? 1 : 0;
      if (na != nb) return nb - na;
      return b.sevRank - a.sevRank;
    });
    _alertBanner(fresh.first);
  }

  bool _isNearbyAlarm(TbAlarm a) {
    final w = _wearerByName(a.originatorName);
    return w?.distanceM != null && w!.distanceM! <= kNearbyRadiusM;
  }

  Wearer? _wearerByName(String name) {
    for (final w in _wearers) {
      if (w.name == name) return w;
    }
    return null;
  }

  Future<void> _alertBanner(TbAlarm a) async {
    final nearby = _isNearbyAlarm(a);
    final hasVib = await Vibration.hasVibrator();
    if (hasVib) {
      Vibration.vibrate(
          pattern: a.severity == 'CRITICAL'
              ? [0, 500, 120, 500, 120, 800]
              : [0, 350, 150, 350]);
    }
    _showBanner(a, nearby);
  }

  void _showBanner(TbAlarm a, bool nearby) {
    _banner?.remove();
    final w = _wearerByName(a.originatorName);
    final dist = w?.distanceM;
    _banner = OverlayEntry(
      builder: (ctx) => Positioned(
        top: MediaQuery.of(ctx).padding.top + 8,
        left: 12,
        right: 12,
        child: Material(
          color: Colors.transparent,
          child: Container(
            padding: const EdgeInsets.fromLTRB(14, 12, 8, 12),
            decoration: BoxDecoration(
              color: a.severity == 'CRITICAL'
                  ? const Color(0xFFb71c1c)
                  : const Color(0xFFb26a00),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(
                  color: nearby ? Colors.amberAccent : Colors.white24,
                  width: nearby ? 2.5 : 1),
            ),
            child: Row(
              children: [
                Icon(nearby ? Icons.my_location : Icons.medical_services,
                    color: Colors.white, size: 28),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '${nearby ? "【附近】" : ""}${prettyAlarmType(a.type)} · ${w?.shortName ?? a.originatorName}',
                        style: const TextStyle(
                            color: Colors.white,
                            fontSize: 15,
                            fontWeight: FontWeight.w700),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        [
                          a.severity,
                          if (a.hrMax != null) 'HR ${a.hrMax!.toStringAsFixed(0)}',
                          if (dist != null) '${dist.toStringAsFixed(0)} m',
                        ].join('  ·  '),
                        style: const TextStyle(color: Colors.white70, fontSize: 12),
                      ),
                    ],
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.close, color: Colors.white),
                  onPressed: () {
                    _banner?.remove();
                    _banner = null;
                  },
                ),
              ],
            ),
          ),
        ),
      ),
    );
    Overlay.of(context).insert(_banner!);
    Future.delayed(const Duration(seconds: 8), () {
      _banner?.remove();
      _banner = null;
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    _gpsTimer?.cancel();
    _banner?.remove();
    super.dispose();
  }

  List<Wearer> get _sortedWearers {
    final list = [..._wearers];
    list.sort((a, b) {
      // alarmed first, then nearby, then by name
      if (a.hasAlarm != b.hasAlarm) return a.hasAlarm ? -1 : 1;
      final da = a.distanceM ?? double.infinity;
      final db = b.distanceM ?? double.infinity;
      if (da != db) return da.compareTo(db);
      return a.shortName.compareTo(b.shortName);
    });
    return list;
  }

  @override
  Widget build(BuildContext context) {
    final pages = [
      WearersTab(
          wearers: _sortedWearers, error: _pollError, onDispatch: _sendCommand),
      MapTab(wearers: _wearers, me: _me),
      AlarmsTab(alarms: _alarms, wearerByName: _wearerByName),
    ];
    final alarmCount = _alarms.length;
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Guardian Console', style: TextStyle(fontSize: 16)),
            Text(widget.session.email,
                style: const TextStyle(fontSize: 11, color: Colors.white54)),
          ],
        ),
        actions: [
          IconButton(
            tooltip: '切換帳號 / 登出',
            icon: const Icon(Icons.switch_account),
            onPressed: widget.onLogout,
          ),
        ],
      ),
      body: pages[_tab],
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: [
          NavigationDestination(
              icon: Badge(
                isLabelVisible: _wearers.any((w) => w.hasAlarm),
                child: const Icon(Icons.watch),
              ),
              label: '配戴者'),
          const NavigationDestination(icon: Icon(Icons.map), label: '地圖'),
          NavigationDestination(
              icon: Badge(
                isLabelVisible: alarmCount > 0,
                label: Text('$alarmCount'),
                child: const Icon(Icons.notifications_active),
              ),
              label: '告警'),
        ],
      ),
    );
  }
}

// ---------- Tab 1: Wearers list (control center) ----------

class WearersTab extends StatelessWidget {
  final List<Wearer> wearers;
  final String? error;
  final Future<void> Function(Wearer, String) onDispatch;
  const WearersTab(
      {super.key,
      required this.wearers,
      required this.onDispatch,
      this.error});

  @override
  Widget build(BuildContext context) {
    if (wearers.isEmpty) {
      return Center(
        child: Text(error ?? '沒有可監看的配戴者',
            style: const TextStyle(color: Colors.white54)),
      );
    }
    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        if (error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text('連線異常：$error',
                style: const TextStyle(color: Color(0xFFff8a80), fontSize: 12)),
          ),
        ...wearers.map(_card),
      ],
    );
  }

  Widget _card(Wearer w) {
    final nearby = w.distanceM != null && w.distanceM! <= kNearbyRadiusM;
    final Color accent = w.isCritical
        ? const Color(0xFFd32f2f)
        : w.hasAlarm
            ? const Color(0xFFf57c00)
            : (w.active ? const Color(0xFF388e3c) : Colors.grey);
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF14203a),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: accent.withValues(alpha: 0.6), width: 1.4),
      ),
      child: Column(
        children: [
          Row(
            children: [
              CircleAvatar(
                backgroundColor: accent.withValues(alpha: 0.2),
                child: Icon(w.hasAlarm ? Icons.warning_amber : Icons.watch,
                    color: accent),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Text(w.shortName,
                            style: const TextStyle(
                                fontSize: 16, fontWeight: FontWeight.w700)),
                        const SizedBox(width: 8),
                        if (nearby)
                          Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 6, vertical: 1),
                            decoration: BoxDecoration(
                                color: Colors.amber.withValues(alpha: 0.2),
                                borderRadius: BorderRadius.circular(6)),
                            child: const Text('附近',
                                style:
                                    TextStyle(color: Colors.amber, fontSize: 10)),
                          ),
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      [
                        prettySituation(w.situation),
                        if (w.bpm != null) 'HR ${w.bpm!.toStringAsFixed(0)}',
                        if (w.distanceM != null)
                          '${w.distanceM!.toStringAsFixed(0)} m',
                        w.active ? '在線' : '離線',
                      ].join('  ·  '),
                      style: const TextStyle(color: Colors.white70, fontSize: 12),
                    ),
                  ],
                ),
              ),
              if (w.hasAlarm)
                Text(
                  w.isCritical ? 'SOS' : '注意',
                  style: TextStyle(
                      color: accent, fontSize: 13, fontWeight: FontWeight.w800),
                ),
            ],
          ),
          // B4: on an active alarm, the guardian can dispatch ("我看到了/前往中")
          // which writes a TB shared attribute the wearer's watch reads.
          if (w.hasAlarm)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  OutlinedButton.icon(
                    icon: const Icon(Icons.directions_run, size: 16),
                    label: const Text('我看到了 · 前往中'),
                    style: OutlinedButton.styleFrom(
                        foregroundColor: const Color(0xFF38d3ff)),
                    onPressed: () => onDispatch(w, 'dispatch'),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

// ---------- Tab 2: Map ----------

class MapTab extends StatelessWidget {
  final List<Wearer> wearers;
  final Position? me;
  const MapTab({super.key, required this.wearers, this.me});

  @override
  Widget build(BuildContext context) {
    final located = wearers.where((w) => w.lat != null && w.lon != null).toList();
    final center = me != null
        ? LatLng(me!.latitude, me!.longitude)
        : (located.isNotEmpty
            ? LatLng(located.first.lat!, located.first.lon!)
            : const LatLng(25.0335, 121.5645));
    return FlutterMap(
      options: MapOptions(initialCenter: center, initialZoom: 16),
      children: [
        TileLayer(
          urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
          userAgentPackageName: 'com.guardian.phone_app',
        ),
        MarkerLayer(
          markers: [
            if (me != null)
              Marker(
                point: LatLng(me!.latitude, me!.longitude),
                width: 60,
                height: 60,
                child: _pin(Colors.blueAccent, '我', Icons.my_location),
              ),
            ...located.map((w) {
              final color = w.isCritical
                  ? Colors.red
                  : w.hasAlarm
                      ? Colors.orange
                      : Colors.green;
              return Marker(
                point: LatLng(w.lat!, w.lon!),
                width: 60,
                height: 60,
                child: _pin(
                    color, w.shortName, w.hasAlarm ? Icons.warning : Icons.watch),
              );
            }),
          ],
        ),
      ],
    );
  }

  Widget _pin(Color color, String label, IconData icon) => Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: color, size: 30),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
            color: Colors.black.withValues(alpha: 0.6),
            child:
                Text(label, style: const TextStyle(fontSize: 9, color: Colors.white)),
          ),
        ],
      );
}

// ---------- Tab 3: Alarms ----------

class AlarmsTab extends StatelessWidget {
  final List<TbAlarm> alarms;
  final Wearer? Function(String) wearerByName;
  const AlarmsTab({super.key, required this.alarms, required this.wearerByName});

  @override
  Widget build(BuildContext context) {
    if (alarms.isEmpty) {
      return const Center(
        child: Text('目前沒有作用中的告警', style: TextStyle(color: Colors.white54)),
      );
    }
    return ListView.separated(
      itemCount: alarms.length,
      separatorBuilder: (_, _) => const Divider(height: 1),
      itemBuilder: (ctx, i) {
        final a = alarms[i];
        final w = wearerByName(a.originatorName);
        final dist = w?.distanceM;
        final nearby = dist != null && dist <= kNearbyRadiusM;
        final color = a.severity == 'CRITICAL'
            ? const Color(0xFFd32f2f)
            : a.severity == 'MAJOR'
                ? const Color(0xFFf57c00)
                : const Color(0xFFfbc02d);
        final t = DateTime.fromMillisecondsSinceEpoch(a.createdTime.round())
            .toLocal()
            .toString()
            .split('.')
            .first;
        return ListTile(
          leading: CircleAvatar(
            backgroundColor: color.withValues(alpha: 0.2),
            child: Icon(Icons.notifications_active, color: color),
          ),
          title: Text(
            '${nearby ? "【附近】" : ""}${prettyAlarmType(a.type)} · ${w?.shortName ?? a.originatorName}',
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
          subtitle: Text(
            '${[
              a.severity,
              if (a.hrMax != null) 'HR ${a.hrMax!.toStringAsFixed(0)}',
              if (dist != null) '${dist.toStringAsFixed(0)} m',
            ].join('  ·  ')}\n$t',
            style: const TextStyle(fontSize: 12),
          ),
          isThreeLine: true,
          trailing: Text(a.status,
              style: const TextStyle(color: Colors.white38, fontSize: 10)),
        );
      },
    );
  }
}
