import java.io.File
import java.time.LocalDate

plugins {
    alias(libs.plugins.android.application)
}

// The build version comes from the repository's VERSION file only (06-install 8):
// YYYYMMDD.HHMM -> versionName as it is; versionCode by tools/versioning.py
// android_version_code: YY*10^7 + day_of_year*10^4 + HHMM (always increasing, fits an Int).
val zlVersion: String = File(rootDir.parentFile, "VERSION").readText().trim()
val zlVersionCode: Int = run {
    val (day, time) = zlVersion.split(".")
    val date = LocalDate.of(day.take(4).toInt(), day.substring(4, 6).toInt(), day.takeLast(2).toInt())
    (date.year % 100) * 10_000_000 + date.dayOfYear * 10_000 + time.toInt()
}

android {
    namespace = "com.zeroloss.receiver"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.zeroloss.receiver"
        minSdk = 29 // Android 10 (05-standards 4.5); 16 and 17 are the verified ones
        targetSdk = 37 // Android 17
        versionCode = zlVersionCode
        versionName = zlVersion
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.ui)
    testImplementation(libs.junit)
    testImplementation(libs.orgjson) // the real org.json: the Android one is a stub in unit tests
}
