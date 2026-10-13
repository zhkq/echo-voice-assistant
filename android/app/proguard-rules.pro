# Keep Retrofit/OkHttp working
-keepattributes Signature
-keepattributes *Annotation*
-dontwarn okhttp3.**
-dontwarn retrofit2.**
-keep class com.echo.contact.data.dto.** { *; }
