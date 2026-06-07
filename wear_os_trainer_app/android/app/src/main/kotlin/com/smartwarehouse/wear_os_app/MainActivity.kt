package com.smartwarehouse.wear_os_app

import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.health.services.client.HealthServices
import androidx.health.services.client.MeasureCallback
import androidx.health.services.client.MeasureClient
import androidx.health.services.client.data.Availability
import androidx.health.services.client.data.DataPointContainer
import androidx.health.services.client.data.DataType
import androidx.health.services.client.data.DeltaDataType
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
	private lateinit var methodChannel: MethodChannel
	private var heartSignalCount = 0
	private var lastRawBpm = -1.0
	private var lastSignalSource = "health_services"
	private var lastSignalAtMs: Long? = null
	private var isListening = false
	private var pendingPermissionResult: MethodChannel.Result? = null
	private var measureClient: MeasureClient? = null
	private var measureCallback: MeasureCallback? = null
	private var healthServicesRegistered = false
	private var lastStartByHealthServices = false
	private var lastStartReason = "not_started"

	override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
		super.configureFlutterEngine(flutterEngine)

		measureClient = runCatching { HealthServices.getClient(this).measureClient }.getOrNull()

		methodChannel = MethodChannel(
			flutterEngine.dartExecutor.binaryMessenger,
			HEART_RATE_CHANNEL,
		)

		methodChannel.setMethodCallHandler { call: MethodCall, result: MethodChannel.Result ->
			when (call.method) {
				"requestHeartRatePermission", "requestBodySensorsPermission" -> {
					requestHeartRatePermission(result)
				}

				"startHeartRate" -> {
					result.success(startHeartRateUpdates())
				}

				"heartDiagnostics" -> {
					result.success(heartDiagnostics())
				}

				"stopHeartRate" -> {
					stopHeartRateUpdates()
					result.success(true)
				}

				else -> {
					result.notImplemented()
				}
			}
		}
	}

	private fun requestHeartRatePermission(result: MethodChannel.Result) {
		if (hasHeartRatePermission()) {
			result.success(mapOf("granted" to true, "reason" to "already_granted"))
			return
		}

		if (pendingPermissionResult != null) {
			result.success(mapOf("granted" to false, "reason" to "request_in_progress"))
			return
		}

		pendingPermissionResult = result
		val permissionsToRequest = arrayOf(PERMISSION_READ_HEART_RATE)
		ActivityCompat.requestPermissions(
			this,
			permissionsToRequest,
			REQUEST_HEART_RATE_PERMISSION,
		)
	}

	private fun startHeartRateUpdates(): Map<String, Any> {
		stopHeartRateUpdates()
		heartSignalCount = 0
		lastRawBpm = -1.0
		lastSignalSource = "health_services"
		lastSignalAtMs = null
		lastStartByHealthServices = false
		lastStartReason = "initializing"

		Log.i(
			TAG,
			"startHeartRateUpdates: heartPerm=${hasHeartRatePermission()} healthClient=${measureClient != null}",
		)

		if (!hasHeartRatePermission()) {
			lastStartReason = "Heart permission is missing (READ_HEART_RATE)."
			runOnUiThread {
				methodChannel.invokeMethod("onHeartRateAvailability", "permission_missing")
			}
			return mapOf(
				"started" to false,
				"reason" to "Heart permission is missing (READ_HEART_RATE).",
			)
		}

		val startedByHealthServices = startHealthServicesHeartRate()
		lastStartByHealthServices = startedByHealthServices
		lastStartReason = "health_services=$startedByHealthServices"

		runOnUiThread {
			methodChannel.invokeMethod(
				"onHeartRateAvailability",
				"startup: $lastStartReason",
			)
		}

		if (!startedByHealthServices) {
			lastStartReason = "Health Services callback registration failed."
			Log.w(TAG, "startHeartRateUpdates failed: $lastStartReason")
			return mapOf(
				"started" to false,
				"reason" to "Health Services callback registration failed.",
			)
		}

		isListening = startedByHealthServices
		Log.i(TAG, "startHeartRateUpdates success: $lastStartReason")

		return mapOf(
			"started" to true,
			"reason" to "Listening. health_services=$startedByHealthServices",
		)
	}

	private fun stopHeartRateUpdates() {
		stopHealthServicesHeartRate()
		isListening = false
	}

	private fun startHealthServicesHeartRate(): Boolean {
		val client = measureClient ?: return false

		val callback = object : MeasureCallback {
			override fun onAvailabilityChanged(
				dataType: DeltaDataType<*, *>,
				availability: Availability,
			) {
				if (dataType == DataType.HEART_RATE_BPM) {
					Log.d(TAG, "HealthServices availability=${availability::class.java.simpleName}")
					runOnUiThread {
						methodChannel.invokeMethod(
							"onHeartRateAvailability",
							availability::class.java.simpleName,
						)
					}
				}
			}

			override fun onDataReceived(data: DataPointContainer) {
				val heartRateDataPoints = data.getData(DataType.HEART_RATE_BPM)
				for (sample in heartRateDataPoints) {
					val bpm = sample.value
					emitHeartSignal(
						rawBpm = bpm,
						source = "health_services",
						accepted = bpm > 0.0,
						accuracy = null,
					)
					if (bpm > 0.0) {
						emitHeartRate(bpm, "health_services")
					}
				}
			}
		}

		measureCallback = callback
		return try {
			client.registerMeasureCallback(DataType.HEART_RATE_BPM, callback)
			healthServicesRegistered = true
			Log.i(TAG, "HealthServices registerMeasureCallback success")
			true
		} catch (_: Throwable) {
			healthServicesRegistered = false
			measureCallback = null
			Log.w(TAG, "HealthServices registerMeasureCallback failed")
			false
		}
	}

	private fun stopHealthServicesHeartRate() {
		val client = measureClient
		val callback = measureCallback
		if (client != null && callback != null) {
			runCatching { tryUnregisterMeasureCallback(client, callback) }
		}
		measureCallback = null
		healthServicesRegistered = false
	}

	private fun tryUnregisterMeasureCallback(client: MeasureClient, callback: MeasureCallback) {
		val methods = client.javaClass.methods
		for (method in methods) {
			when (method.name) {
				"unregisterMeasureCallback", "clearMeasureCallback", "clearCallback" -> {
					try {
						val parameterTypes = method.parameterTypes
						if (parameterTypes.size == 1 &&
							MeasureCallback::class.java.isAssignableFrom(parameterTypes[0])
						) {
							method.invoke(client, callback)
							return
						}

						if (parameterTypes.size == 2 &&
							DeltaDataType::class.java.isAssignableFrom(parameterTypes[0]) &&
							MeasureCallback::class.java.isAssignableFrom(parameterTypes[1])
						) {
							method.invoke(client, DataType.HEART_RATE_BPM, callback)
							return
						}
					} catch (_: Throwable) {
						// Try another candidate method.
					}
				}
			}
		}
	}

	// Emits a Map so the Dart _hrBuffer gets {timestamp, bpm, accuracy} entries
	// compatible with the posture API payload format.
	private fun emitHeartRate(bpm: Double, source: String) {
		val timestampS = System.currentTimeMillis() / 1000.0
		runOnUiThread {
			methodChannel.invokeMethod(
				"onHeartRate",
				mapOf(
					"bpm"       to bpm,
					"timestamp" to timestampS,
					"accuracy"  to 3,  // Health Services always fires on high-confidence readings
				),
			)
			methodChannel.invokeMethod("onHeartRateSource", source)
		}
	}

	private fun emitHeartSignal(
		rawBpm: Double,
		source: String,
		accepted: Boolean,
		accuracy: Int?,
	) {
		heartSignalCount += 1
		lastRawBpm = rawBpm
		lastSignalSource = source
		lastSignalAtMs = System.currentTimeMillis()
		if (heartSignalCount <= 5 || heartSignalCount % 20 == 0) {
			Log.d(
				TAG,
				"signal#$heartSignalCount source=$source raw=$rawBpm accepted=$accepted accuracy=$accuracy",
			)
		}

		runOnUiThread {
			methodChannel.invokeMethod(
				"onHeartSignal",
				mapOf(
					"count"    to heartSignalCount,
					"rawBpm"   to rawBpm,
					"source"   to source,
					"accepted" to accepted,
					"accuracy" to accuracy,
				),
			)
		}
	}

	private fun heartDiagnostics(): Map<String, Any> {
		val requestedPermissions = arrayOf(PERMISSION_READ_HEART_RATE)
		val deniedPermanently =
			!hasHeartRatePermission() && requestedPermissions.all { permission ->
				!ActivityCompat.shouldShowRequestPermissionRationale(this, permission)
			}
		val lastSignalAgeMs = if (lastSignalAtMs == null) {
			-1L
		} else {
			System.currentTimeMillis() - (lastSignalAtMs ?: 0L)
		}

		return mapOf(
			"sdkInt"                    to Build.VERSION.SDK_INT,
			"hasBodySensorPermission"   to false,
			"hasReadHeartRatePermission" to hasReadHeartRatePermission(),
			"hasHeartPermission"        to hasHeartRatePermission(),
			"bodySensorsDeniedPermanently" to deniedPermanently,
			"hasHeartRateSensor"        to true,
			"hasHeartBeatSensor"        to false,
			"hasHealthServicesClient"   to (measureClient != null),
			"healthServicesRegistered"  to healthServicesRegistered,
			"isListening"               to isListening,
			"heartSignalCount"          to heartSignalCount,
			"lastRawBpm"                to lastRawBpm,
			"lastSignalSource"          to lastSignalSource,
			"lastSignalAgeMs"           to lastSignalAgeMs,
			"lastStartByHealthServices" to lastStartByHealthServices,
			"lastStartBySensors"        to false,
			"lastStartReason"           to lastStartReason,
		)
	}

	override fun onRequestPermissionsResult(
		requestCode: Int,
		permissions: Array<out String>,
		grantResults: IntArray,
	) {
		super.onRequestPermissionsResult(requestCode, permissions, grantResults)

		if (requestCode != REQUEST_HEART_RATE_PERMISSION) {
			return
		}

		val result = pendingPermissionResult
		pendingPermissionResult = null
		if (result == null) {
			return
		}

		val granted = hasHeartRatePermission()
		val deniedPermanently =
			!granted && permissions.all { permission ->
				!ActivityCompat.shouldShowRequestPermissionRationale(this, permission)
			}

		val reason = when {
			granted          -> "granted"
			deniedPermanently -> "denied_permanently"
			else             -> "denied"
		}

		result.success(mapOf("granted" to granted, "reason" to reason))
	}

	private fun hasReadHeartRatePermission(): Boolean {
		return ContextCompat.checkSelfPermission(
			this,
			PERMISSION_READ_HEART_RATE,
		) == PackageManager.PERMISSION_GRANTED
	}

	private fun hasHeartRatePermission(): Boolean = hasReadHeartRatePermission()

	override fun onDestroy() {
		stopHeartRateUpdates()
		super.onDestroy()
	}

	companion object {
		private const val HEART_RATE_CHANNEL = "wear_os/heart_rate"
		private const val REQUEST_HEART_RATE_PERMISSION = 9031
		private const val TAG = "WearOsApp"
		private const val PERMISSION_READ_HEART_RATE = "android.permission.health.READ_HEART_RATE"
	}
}
