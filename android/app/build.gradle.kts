import java.util.Properties
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// ---------------------------------------------------------------------------------------
// Release signing
//
// The upload/release keystore never lives in this repository. By default it is read from
// ~/WubarrkCODING/DLNE_RELEASE_KEYS/android/keystore.properties; another location can be
// given with -Pdlne.keystoreProperties=/path/to/keystore.properties or the environment
// variable DLNE_KEYSTORE_PROPERTIES. The file holds four keys:
//     storeFile=/absolute/path/to/dlne-release.jks
//     storePassword=...
//     keyAlias=dlne
//     keyPassword=...
// When no such file exists (any contributor's machine), release builds are simply left
// unsigned instead of failing.
// ---------------------------------------------------------------------------------------
val keystorePropertiesFile: File = run {
    val override = providers.gradleProperty("dlne.keystoreProperties").orNull
        ?: System.getenv("DLNE_KEYSTORE_PROPERTIES")
    if (!override.isNullOrBlank()) {
        File(override)
    } else {
        File(System.getProperty("user.home"), "WubarrkCODING/DLNE_RELEASE_KEYS/android/keystore.properties")
    }
}
val keystoreProperties: Properties? = keystorePropertiesFile.takeIf { it.isFile }?.let { file ->
    Properties().apply { file.inputStream().use { load(it) } }
}

android {
    namespace = "io.dlne.chat"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.dlne.chat"
        minSdk = 26
        targetSdk = 36
        // Bump versionCode by one for every upload to Google Play (it must always increase);
        // versionName is what people see. See android/README.md.
        versionCode = 2
        versionName = "1.0.1"
    }

    signingConfigs {
        if (keystoreProperties != null) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    // The dependency-metadata block is an opaque blob signed for Google; keep it out of the
    // APK (sideloaded builds stay reproducible and plain) but in the Play bundle, where Play
    // uses it to warn about vulnerable library versions. It contains no user data.
    dependenciesInfo {
        includeInApk = false
        includeInBundle = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

tasks.withType<Test>().configureEach {
    // The live smoke test (LiveSmokeTest) talks to the real DLNE service, so it only runs
    // when asked for: ./gradlew :app:testDebugUnitTest -Pdlne.smoke=true
    systemProperty("dlne.smoke", providers.gradleProperty("dlne.smoke").getOrElse("false"))
    systemProperty("dlne.smoke.url", providers.gradleProperty("dlne.smoke.url").getOrElse(""))
    testLogging {
        events("passed", "skipped", "failed")
        showStandardStreams = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.foundation)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)

    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}
