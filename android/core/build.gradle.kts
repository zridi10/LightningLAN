import org.jetbrains.kotlin.gradle.dsl.JvmTarget

// The LightningLAN protocol in plain Kotlin: no Android classes, so it runs (and is tested)
// on a desktop JVM too. Only APIs that Android 8.0 (API 26) has may be used here.
plugins {
    id("org.jetbrains.kotlin.jvm")
    id("com.android.lint")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
        freeCompilerArgs.add("-Xjdk-release=17") // no newer JDK APIs (the app's lint checks Android's)
    }
}

dependencies {
    api("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    testImplementation(kotlin("test"))
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.3")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
    // The interop tests run real desktop peers from this repository's Python code.
    val repo = rootProject.projectDir.parentFile
    systemProperty("lightninglan.repo", repo.absolutePath)
    System.getenv("LIGHTNINGLAN_PYTHON")?.let { systemProperty("lightninglan.python", it) }
    testLogging {
        events("passed", "skipped", "failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        showStandardStreams = false
    }
}
