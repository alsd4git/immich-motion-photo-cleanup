BEGIN TRANSACTION READ ONLY;
SELECT json_build_object(
  'id', a.id,
  'owner_id', a."ownerId",
  'type', a.type,
  'path', a."originalPath",
  'name', a."originalFileName",
  'linked_video_id', a."livePhotoVideoId",
  'captured_at', a."localDateTime",
  'cid', e."livePhotoCID",
  'camera_make', e.make,
  'camera_model', e.model,
  'visibility', a.visibility
)
FROM asset a
LEFT JOIN asset_exif e ON e."assetId" = a.id
WHERE a."deletedAt" IS NULL
ORDER BY a."ownerId", a.id;
COMMIT;
