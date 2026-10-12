// SPDX-License-Identifier: GPL-3.0-or-later
import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
}

// Release signing. K1000_KEYSTORE_PROPERTIES names a properties file that lives OUTSIDE the repo
// (storeFile, relative to that file or absolute; storePassword, keyAlias, keyPassword). Without it
// the release APK is built unsigned.
val keystorePropsFile: File? = System.getenv("K1000_KEYSTORE_PROPERTIES")
    ?.takeIf { it.isNotBlank() }
    ?.let { File(it) }
val keystoreProps: Properties? = keystorePropsFile
    ?.let { f -> Properties().apply { FileInputStream(f).use { load(it) } } }

android {
    namespace = "gr.trentesept.k1000"
    compileSdk = 37

    defaultConfig {
        applicationId = "gr.trentesept.k1000"
        minSdk = 29          // Android 10: WifiNetworkSpecifier
        targetSdk = 37       // Android 17 (stable since 2026-06)
        versionCode = 2
        versionName = "0.1.1"
        // The smoke build (emulator test) flips these; the real app keeps them.
        buildConfigField("boolean", "SMOKE", "false")
    }

    signingConfigs {
        if (keystoreProps != null) {
            create("release") {
                storeFile = keystorePropsFile!!.parentFile.resolve(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
                enableV1Signing = false   // minSdk 29: APK Signature Scheme v2/v3 only
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
        // Emulator smoke test only (android/tools/smoke.sh): bundles a throwaway test CA instead of
        // the K1000 CA (src/smoke/res/raw), binds to whatever Wi-Fi the emulator has instead of
        // joining K1000, and enables WebView remote debugging. Never distributed.
        create("smoke") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".smoke"
            versionNameSuffix = "-smoke"
            buildConfigField("boolean", "SMOKE", "true")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        lintConfig = file("lint.xml")
        abortOnError = true
        checkReleaseBuilds = true
    }

    // No Google-encrypted dependency metadata block in the APK (it is a public GPL app).
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }
}

tasks.withType<JavaCompile>().configureEach {
    options.compilerArgs.addAll(listOf("-Xlint:deprecation", "-Xlint:unchecked"))
}

dependencies {
    // QR scanner: ZXing Android Embedded (Apache-2.0, no Play Services) + ZXing core (Apache-2.0).
    implementation("com.journeyapps:zxing-android-embedded:4.3.0") { isTransitive = false }
    implementation("com.google.zxing:core:3.5.4")

    testImplementation("junit:junit:4.13.2")
}

// The smoke build's CA: -Pk1000.smokeCa=<throwaway test CA, PEM> (android/tools/smoke.sh makes a
// fresh one per run). It overrides res/raw/k1000_ca.pem in the smoke build only. Without the
// property the smoke build keeps the real K1000 CA (and the emulator test then fails closed).
abstract class SmokeCaTask : DefaultTask() {
    @get:InputFile
    @get:Optional
    abstract val ca: RegularFileProperty

    @get:OutputDirectory
    abstract val outputDir: DirectoryProperty

    @TaskAction
    fun copy() {
        val raw = outputDir.get().dir("raw").asFile
        raw.deleteRecursively()
        if (ca.isPresent) {
            raw.mkdirs()
            ca.get().asFile.copyTo(File(raw, "k1000_ca.pem"), overwrite = true)
        }
    }
}

val smokeCa = providers.gradleProperty("k1000.smokeCa")

androidComponents {
    onVariants(selector().withBuildType("smoke")) { variant ->
        val task = tasks.register<SmokeCaTask>("${variant.name}SmokeCa") {
            ca.set(layout.file(smokeCa.map { File(it) }))
        }
        variant.sources.res?.addGeneratedSourceDirectory(task, SmokeCaTask::outputDir)
    }
}
