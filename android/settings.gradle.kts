// SPDX-License-Identifier: GPL-3.0-or-later
// K1000 Android shell: a thin WebView around the PWA the box serves (docs/09-android.md).

// Every dependency (the plugin included) comes from Google's Maven or Maven Central, and is
// checked against gradle/verification-metadata.xml (sha256) before Gradle uses it.
pluginManagement {
    repositories {
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "K1000"
include(":app")
