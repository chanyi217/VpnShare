import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.vpnshare.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.vpnshare.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 3
        versionName = "1.2"
    }

    // ★ 签名配置：keystore 不随仓库分发（见 .gitignore），
    //   所以这里做优雅降级——本地有 keystore 就用它签名，没有就走 debug 签名，
    //   保证 clone 下来开箱可构建。
    //
    //   想用自己的签名：把 vpnshare.jks 放到 android-app/keystore/ 下，
    //   并创建 android-app/keystore.properties：
    //       storeFile=../keystore/vpnshare.jks
    //       storePassword=xxx
    //       keyAlias=xxx
    //       keyPassword=xxx
    val ksPropsFile = rootProject.file("keystore.properties")
    val ksProps = Properties()
    if (ksPropsFile.exists()) {
        ksPropsFile.inputStream().use { ksProps.load(it) }
    }
    // 兼容：keystore 文件存在但没有 properties 时，允许用环境变量覆盖密码
    val hasKs = file("../keystore/vpnshare.jks").exists()

    signingConfigs {
        if (hasKs) {
            create("release") {
                storeFile = file(ksProps.getProperty("storeFile") ?: "../keystore/vpnshare.jks")
                storePassword = ksProps.getProperty("storePassword")
                    ?: System.getenv("VPN_SHARE_KS_PWD") ?: ""
                keyAlias = ksProps.getProperty("keyAlias") ?: "vpnshare"
                keyPassword = ksProps.getProperty("keyPassword")
                    ?: System.getenv("VPN_SHARE_KS_PWD") ?: ""
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // keystore 缺失时退回 debug 签名，不让构建失败
            signingConfig = if (hasKs) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.findByName("debug")
            }
        }
        debug {
            applicationIdSuffix = ".debug"
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
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
