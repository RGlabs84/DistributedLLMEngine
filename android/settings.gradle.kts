// The DLNE Android app: one module (:app). Plugins and libraries come only from
// Google's Maven and Maven Central; versions are pinned in gradle/libs.versions.toml.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "DLNE"
include(":app")
