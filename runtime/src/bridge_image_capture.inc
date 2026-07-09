    auto sdk_capture_front_from_user_image(Reflection& ref,
                                           const SdkContext& ctx,
                                           const SdkNativeFrontSampleResult& native_front,
                                           const std::string& rgba_b64,
                                           int image_width,
                                           int image_height) -> SdkFrontCaptureResult
    {
        SdkFrontCaptureResult out{};
        if (native_front.samples.empty())
        {
            out.failure = "user_image_no_surface_samples";
            return out;
        }
        if (rgba_b64.empty() || image_width <= 0 || image_height <= 0)
        {
            out.failure = "user_image_payload_invalid";
            return out;
        }

        const auto rgba_bytes = base64_decode(rgba_b64);
        const auto expected_bytes = static_cast<std::size_t>(image_width) * static_cast<std::size_t>(image_height) * 4;
        if (rgba_bytes.size() < expected_bytes)
        {
            out.failure = "user_image_decode_size_mismatch";
            return out;
        }

        out.width = image_width;
        out.height = image_height;
        out.capture_resolution_source = "user_image";
        out.capture_aspect = static_cast<double>(std::max(1, image_width)) /
                             static_cast<double>(std::max(1, image_height));
        const auto viewport = sdk_get_viewport_info(ref, ctx);
        out.viewport_width = viewport.width;
        out.viewport_height = viewport.height;
        out.capture_scale_x = static_cast<double>(image_width) /
                              static_cast<double>(std::max(1, viewport.width));
        out.capture_scale_y = static_cast<double>(image_height) /
                              static_cast<double>(std::max(1, viewport.height));
        out.viewport_aspect = static_cast<double>(std::max(1, viewport.width)) /
                              static_cast<double>(std::max(1, viewport.height));
        if (out.viewport_width <= 0 || out.viewport_height <= 0)
        {
            out.failure = "user_image_viewport_unavailable";
            return out;
        }

        const auto center_ray = sdk_deproject_screen_position(
            ref, ctx, static_cast<double>(out.viewport_width) * 0.5, static_cast<double>(out.viewport_height) * 0.5);
        if (!center_ray.ok)
        {
            out.failure = "user_image_camera_deproject_failed:" + center_ray.failure;
            return out;
        }

        auto capture_location = center_ray.location;
        auto capture_direction = center_ray.direction;
        bool deproject_fov_valid = false;
        const auto left_ray = sdk_deproject_screen_position(ref, ctx, 0.0, static_cast<double>(out.viewport_height) * 0.5);
        const auto right_ray = sdk_deproject_screen_position(
            ref, ctx, static_cast<double>(std::max(1, out.viewport_width - 1)), static_cast<double>(out.viewport_height) * 0.5);
        if (left_ray.ok && right_ray.ok)
        {
            const auto left_dir = sdk_vec_normalize(left_ray.direction);
            const auto right_dir = sdk_vec_normalize(right_ray.direction);
            const auto dot = std::max(-1.0, std::min(1.0, sdk_vec_dot(left_dir, right_dir)));
            const auto fov = std::acos(dot) * 180.0 / 3.14159265358979323846;
            if (std::isfinite(fov) && fov >= 10.0 && fov <= 150.0)
            {
                out.capture_fov = fov;
                deproject_fov_valid = true;
            }
        }

        std::string camera_failure{};
        out.camera_manager = sdk_call_no_params_return_object(ref, ctx.controller, "GetPlayerCameraManager", camera_failure);
        if (!live_uobject(out.camera_manager) && ctx.controller)
        {
            const auto field_camera_manager = safe_read<std::uintptr_t>(
                ctx.controller + sdk::FieldOffsets::PlayerController_PlayerCameraManager, 0);
            if (live_uobject(field_camera_manager))
            {
                out.camera_manager = field_camera_manager;
            }
        }
        if (live_uobject(out.camera_manager))
        {
            sdk::FVector camera_location{};
            if (sdk_call_no_params_return_vector3(ref, out.camera_manager, "GetCameraLocation", camera_location))
            {
                capture_location = camera_location;
                out.camera_location_used = true;
            }
            sdk::FRotator camera_rotation{};
            if (sdk_call_no_params_return_rotator(ref, out.camera_manager, "GetCameraRotation", camera_rotation))
            {
                const auto camera_forward = sdk_rotator_forward(camera_rotation);
                const auto center_forward = sdk_vec_normalize(center_ray.direction);
                const auto dot = sdk_vec_dot(sdk_vec_normalize(camera_forward), center_forward);
                if (std::isfinite(dot) && dot > 0.80)
                {
                    capture_direction = camera_forward;
                    out.camera_rotation_used = true;
                }
            }
            double camera_fov = 0.0;
            if (sdk_call_no_params_return_number(ref, out.camera_manager, "GetFOVAngle", camera_fov) &&
                std::isfinite(camera_fov) && camera_fov >= 10.0 && camera_fov <= 150.0 && !deproject_fov_valid)
            {
                out.capture_fov = camera_fov;
                out.camera_fov_used = true;
            }
        }
        if (!out.camera_rotation_used && ctx.controller)
        {
            const auto control_rotation = safe_read<sdk::FRotator>(
                ctx.controller + sdk::FieldOffsets::Controller_ControlRotation, sdk::FRotator{});
            const auto control_forward = sdk_rotator_forward(control_rotation);
            const auto center_forward = sdk_vec_normalize(center_ray.direction);
            const auto dot = sdk_vec_dot(sdk_vec_normalize(control_forward), center_forward);
            if (std::isfinite(dot) && dot > 0.80)
            {
                capture_direction = control_forward;
                out.camera_rotation_used = true;
            }
        }

        out.capture_location = capture_location;
        out.capture_direction = sdk_vec_normalize(capture_direction);
        out.projection_backend = "user_image_camera_projection";

        out.capture_pixels.resize(expected_bytes / 4);
        for (std::size_t i = 0; i < expected_bytes; i += 4)
        {
            auto& pixel = out.capture_pixels[i / 4];
            pixel.r = sdk_srgb_to_linear_unit(static_cast<double>(rgba_bytes[i]) / 255.0);
            pixel.g = sdk_srgb_to_linear_unit(static_cast<double>(rgba_bytes[i + 1]) / 255.0);
            pixel.b = sdk_srgb_to_linear_unit(static_cast<double>(rgba_bytes[i + 2]) / 255.0);
            pixel.roughness = 0.65;
            pixel.metallic = 0.0;
        }
        out.capture_pixels_available = true;
        out.bulk_readback_used = true;
        out.image_bulk_calibration_ok = true;
        out.texture_source = "user_image_rgba";

        struct ProjectedFrontSample
        {
            FrontSample surface{};
            int x{0};
            int y{0};
        };
        std::vector<ProjectedFrontSample> projected{};
        projected.reserve(native_front.samples.size());

        const auto capture_forward = sdk_vec_normalize(out.capture_direction);
        sdk::FVector world_up{0.0, 0.0, 1.0};
        auto capture_right = sdk_vec_normalize(sdk_vec_cross(world_up, capture_forward));
        if (sdk_vec_len(capture_right) <= 0.000001)
        {
            world_up = {0.0, 1.0, 0.0};
            capture_right = sdk_vec_normalize(sdk_vec_cross(world_up, capture_forward));
        }
        const auto capture_up = sdk_vec_normalize(sdk_vec_cross(capture_forward, capture_right));
        const double half_fov_radians = out.capture_fov * 3.14159265358979323846 / 360.0;
        const double tan_half_horizontal = std::tan(half_fov_radians);
        const double tan_half_vertical = tan_half_horizontal / std::max(0.001, out.capture_aspect);

        for (const auto& surface : native_front.samples)
        {
            ++out.project_attempts;
            double sx = clamp01(surface.screen_nx) * static_cast<double>(out.width);
            double sy = clamp01(surface.screen_ny) * static_cast<double>(out.height);
            if (surface.has_world_position)
            {
                double viewport_x = 0.0;
                double viewport_y = 0.0;
                if (sdk_project_world_to_screen(ref, ctx, surface.world_position, viewport_x, viewport_y))
                {
                    sx = viewport_x * out.capture_scale_x;
                    sy = viewport_y * out.capture_scale_y;
                }
                else
                {
                    const auto rel = sdk_vec_sub(surface.world_position, out.capture_location);
                    const double depth = sdk_vec_dot(rel, capture_forward);
                    if (std::isfinite(depth) && depth > 0.000001 &&
                        sdk_vec_len(capture_forward) > 0.000001 &&
                        sdk_vec_len(capture_right) > 0.000001 &&
                        sdk_vec_len(capture_up) > 0.000001 &&
                        std::isfinite(tan_half_horizontal) &&
                        std::isfinite(tan_half_vertical) &&
                        tan_half_horizontal > 0.000001 &&
                        tan_half_vertical > 0.000001)
                    {
                        const double right = sdk_vec_dot(rel, capture_right);
                        const double up = sdk_vec_dot(rel, capture_up);
                        const double ndc_x = right / (depth * tan_half_horizontal);
                        const double ndc_y = up / (depth * tan_half_vertical);
                        if (std::isfinite(ndc_x) && std::isfinite(ndc_y))
                        {
                            sx = (ndc_x * 0.5 + 0.5) * static_cast<double>(out.width);
                            sy = (0.5 - ndc_y * 0.5) * static_cast<double>(out.height);
                        }
                    }
                }
            }
            if (sx < 0.0 || sy < 0.0 ||
                sx >= static_cast<double>(out.width) ||
                sy >= static_cast<double>(out.height))
            {
                ++out.project_out_of_view;
                continue;
            }
            ++out.project_success;
            const auto px = std::max(0, std::min(out.width - 1, static_cast<int>(std::round(sx))));
            const auto py = std::max(0, std::min(out.height - 1, static_cast<int>(std::round(sy))));
            auto projected_surface = surface;
            projected_surface.screen_nx = clamp01(sx / static_cast<double>(std::max(1, out.width)));
            projected_surface.screen_ny = clamp01(sy / static_cast<double>(std::max(1, out.height)));
            projected.push_back(ProjectedFrontSample{projected_surface, px, py});
        }

        if (projected.empty())
        {
            out.failure = "user_image_projection_empty";
            return out;
        }

        out.samples.reserve(projected.size());
        for (const auto& projected_sample : projected)
        {
            const auto pixel_index = static_cast<std::size_t>(projected_sample.y) * static_cast<std::size_t>(out.width) +
                                     static_cast<std::size_t>(projected_sample.x);
            if (pixel_index >= out.capture_pixels.size())
            {
                ++out.missing_color;
                continue;
            }
            const auto raw_color = out.capture_pixels[pixel_index];
            FrontSample sample = projected_sample.surface;
            sample.r = clamp01(raw_color.r);
            sample.g = clamp01(raw_color.g);
            sample.b = clamp01(raw_color.b);
            sample.metallic = clamp01(raw_color.metallic);
            sample.roughness = clamp01(raw_color.roughness);
            out.samples.push_back(sample);
            ++out.read_success;
        }

        out.ok = !out.samples.empty();
        out.failure = out.ok ? "ok" : "user_image_sample_colors_empty";
        return out;
    }
