plugins {
    java
}

group = "kills"
version = "1.0"

repositories {
    mavenCentral()
}

dependencies {
    implementation("com.skadistats:clarity:3.1.3")
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

tasks.jar {
    manifest {
        attributes["Main-Class"] = "kills.KillExtractor"
    }
    archiveBaseName.set("kill_extractor")
    archiveVersion.set("")
    from(configurations.runtimeClasspath.get().map { if (it.isDirectory) it else zipTree(it) })
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
}
