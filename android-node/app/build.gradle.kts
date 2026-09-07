plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.farmos.node"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.farmos.node"
        minSdk = 31
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "FARMOS_API_BASE_URL", "\"https://depends-goals-stays-portsmouth.trycloudflare.com\"")
        buildConfigField("boolean", "FARMOS_ALLOW_INSECURE_HTTP", "false")
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            // Staging HTTPS tunnel — no cleartext, no emulator loopback.
            buildConfigField("boolean", "FARMOS_ALLOW_INSECURE_HTTP", "false")
            buildConfigField("String", "FARMOS_API_BASE_URL", "\"https://depends-goals-stays-portsmouth.trycloudflare.com\"")
        }
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            // Production: HTTPS only, never cleartext.
            buildConfigField("boolean", "FARMOS_ALLOW_INSECURE_HTTP", "false")
            buildConfigField("String", "FARMOS_API_BASE_URL", "\"https://farmos.local\"")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.12.2")
    testImplementation("androidx.test:core:1.6.1")
}
