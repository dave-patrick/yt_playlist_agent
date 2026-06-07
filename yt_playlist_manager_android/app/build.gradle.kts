import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.Properties

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
}

val versionPropsFile = file("version.properties")
val versionProps = Properties().apply {
    if (versionPropsFile.exists()) {
        load(FileInputStream(versionPropsFile))
    } else {
        put("major", "0")
        put("minor", "1")
        put("patch", "2")
        put("code", "1")
        versionPropsFile.createNewFile()
        store(FileOutputStream(versionPropsFile), null)
    }
}

var major = versionProps.getProperty("major").toInt()
var minor = versionProps.getProperty("minor").toInt()
var patch = versionProps.getProperty("patch").toInt()
var code = versionProps.getProperty("code", "1").toInt()

val runTasks = gradle.startParameter.taskNames.toString()
if (runTasks.contains("assemble") || runTasks.contains("copy")) {
    minor += 1
    patch = 0
    code += 1
    versionProps.setProperty("minor", minor.toString())
    versionProps.setProperty("patch", patch.toString())
    versionProps.setProperty("code", code.toString())
    versionProps.store(FileOutputStream(versionPropsFile), null)
}

val computedVersionName = "$major.$minor.$patch"

android {
    namespace = "com.example.ytplaylistmanager"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.example.ytplaylistmanager"
        minSdk = 24
        targetSdk = 36
        versionCode = code
        versionName = computedVersionName
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
      compose = true
      aidl = false
      buildConfig = false
      shaders = false
    }

    packaging {
      resources {
        excludes += "/META-INF/{AL2.0,LGPL2.1}"
      }
    }
}



kotlin {
    jvmToolchain(17)
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  implementation("androidx.compose.material:material-icons-extended")
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  // Navigation
  implementation(libs.androidx.navigation3.ui)
  implementation(libs.androidx.navigation3.runtime)
  implementation(libs.androidx.lifecycle.viewmodel.navigation3)
}

val ver = computedVersionName

tasks.register<Copy>("copyDebugApkWithVersion") {
    dependsOn("assembleDebug")
    from(layout.buildDirectory.dir("outputs/apk/debug"))
    include("app-debug.apk")
    into(layout.buildDirectory.dir("outputs/apk/renamed"))
    rename { "yt-playlist-manager-v$ver-debug.apk" }
}

