# R8 rules for the DLNE app (release builds minify and shrink resources).
#
# OkHttp, Okio, coroutines and Compose ship their own consumer rules inside their
# artifacts, so only the app's own reflective edges are listed here.

# Keep file and line numbers in stack traces (names stay obfuscated). The mapping file in
# app/build/outputs/mapping/release/ turns a crash trace back into readable code; upload it
# to Play Console with each release.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# kotlinx.serialization. The app always calls the generated serializer() functions
# directly (no reflection lookups), but these are the library's recommended rules and
# they keep the generated $serializer classes and companions of @Serializable types intact
# under R8 full mode.
-keepattributes RuntimeVisibleAnnotations,AnnotationDefault,InnerClasses,Signature
-if @kotlinx.serialization.Serializable class **
-keepclassmembers class <1> {
    static <1>$Companion Companion;
}
-if @kotlinx.serialization.Serializable class ** {
    static **$* *;
}
-keepclassmembers class <2>$<3> {
    kotlinx.serialization.KSerializer serializer(...);
}
-if @kotlinx.serialization.Serializable class ** {
    public static ** INSTANCE;
}
-keepclassmembers class <1> {
    public static <1> INSTANCE;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class io.dlne.chat.**$$serializer { *; }

# OkHttp's optional TLS providers are not bundled; silence the warnings about them.
-dontwarn org.bouncycastle.jsse.**
-dontwarn org.conscrypt.**
-dontwarn org.openjsse.**
