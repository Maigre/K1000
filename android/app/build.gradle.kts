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

// The bench build (debug only, never distributed) also trusts ONE development host on the LAN:
// -Pk1000.benchHost=<IPv4 or DNS name> (default 192.168.8.54, Thomas's bench box) and
// -Pk1000.benchCa=<that host's CA, PEM> (default: the K1000 CA itself). docs/09-android.md §9.
val benchHost: String = (providers.gradleProperty("k1000.benchHost").orNull ?: "192.168.8.54").trim().lowercase()
if (!Regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$").matches(benchHost) ||
    benchHost == "10.42.0.1" || benchHost == "k1000.home.arpa") {
    throw GradleException("k1000.benchHost: not a usable development host: $benchHost")
}

android {
    namespace = "gr.trentesept.k1000"
    compileSdk = 37

    defaultConfig {
        applicationId = "gr.trentesept.k1000"
        minSdk = 29          // Android 10: WifiNetworkSpecifier
        targetSdk = 37       // Android 17 (stable since 2026-06)
        versionCode = 5
        versionName = "0.2.2"
        // The smoke build (emulator test) flips these; the real app keeps them.
        buildConfigField("boolean", "SMOKE", "false")
        // The bench build's development host; "" = none (release, debug, smoke).
        buildConfigField("String", "BENCH_HOST", "\"\"")
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
        // joining K1000, and enables WebView remote debugging. Never distributed. Minified and
        // shrunk with the release rules, so the bridge checks run on R8's output as the release
        // app's code is (the release APK itself cannot load the smoke server's test-CA page).
        create("smoke") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".smoke"
            versionNameSuffix = "-smoke"
            buildConfigField("boolean", "SMOKE", "true")
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        // Bench only (debug-signed, never distributed): the release app's trust and hosts, plus
        // one development host (k1000.benchHost) with its own CA, so the bench box can be driven
        // over the LAN. Installs beside the release app (its own package), WebView debuggable.
        create("bench") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".bench"
            versionNameSuffix = "-bench"
            buildConfigField("String", "BENCH_HOST", "\"$benchHost\"")
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

// The bench build's trust: the main network security config with one more domain (the bench
// host) trusting raw/k1000_bench_ca (k1000.benchCa, or a copy of the K1000 CA). Generated into
// the bench variant only; release, debug and smoke keep res/xml/network_security_config.xml.
abstract class BenchTrustTask : DefaultTask() {
    @get:Input
    abstract val host: Property<String>

    @get:InputFile
    @get:Optional
    abstract val ca: RegularFileProperty

    @get:InputFile
    abstract val mainCa: RegularFileProperty

    @get:InputFile
    abstract val mainConfig: RegularFileProperty

    @get:OutputDirectory
    abstract val outputDir: DirectoryProperty

    @TaskAction
    fun write() {
        val out = outputDir.get().asFile
        out.deleteRecursively()
        File(out, "raw").mkdirs()
        File(out, "xml").mkdirs()
        val src = if (ca.isPresent) ca.get().asFile else mainCa.get().asFile
        src.copyTo(File(out, "raw/k1000_bench_ca.pem"), overwrite = true)
        val main = mainConfig.get().asFile.readText()
        val end = "</network-security-config>"
        check(main.contains(end)) { "unexpected network_security_config.xml" }
        val bench = """    <!-- BENCH BUILD ONLY: the development host ${host.get()}, its own CA. -->
    <domain-config cleartextTrafficPermitted="false">
        <domain includeSubdomains="false">${host.get()}</domain>
        <trust-anchors>
            <certificates src="@raw/k1000_bench_ca" />
        </trust-anchors>
    </domain-config>
"""
        File(out, "xml/network_security_config.xml").writeText(main.replace(end, bench + end))
    }
}

val benchCa = providers.gradleProperty("k1000.benchCa")

androidComponents {
    onVariants(selector().withBuildType("smoke")) { variant ->
        val task = tasks.register<SmokeCaTask>("${variant.name}SmokeCa") {
            ca.set(layout.file(smokeCa.map { File(it) }))
        }
        variant.sources.res?.addGeneratedSourceDirectory(task, SmokeCaTask::outputDir)
    }
    onVariants(selector().withBuildType("bench")) { variant ->
        val task = tasks.register<BenchTrustTask>("${variant.name}BenchTrust") {
            host.set(benchHost)
            ca.set(layout.file(benchCa.map { File(it) }))
            mainCa.set(layout.projectDirectory.file("src/main/res/raw/k1000_ca.pem"))
            mainConfig.set(layout.projectDirectory.file("src/main/res/xml/network_security_config.xml"))
        }
        variant.sources.res?.addGeneratedSourceDirectory(task, BenchTrustTask::outputDir)
    }
}
