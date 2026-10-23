BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '90s';
WITH file_paths AS (
  SELECT path, array_agg("assetId") AS ids FROM asset_file GROUP BY path
)
SELECT json_build_object(
  'id', a.id, 'owner_id', a."ownerId", 'type', a.type,
  'path', a."originalPath", 'name', a."originalFileName",
  'linked_video_id', a."livePhotoVideoId", 'visibility', a.visibility,
  'status', a.status, 'deleted_at', a."deletedAt", 'updated_at', a."updatedAt",
  'offline', a."isOffline", 'external', a."isExternal" OR a."libraryId" IS NOT NULL,
  'favorite', a."isFavorite", 'stack', a."stackId" IS NOT NULL,
  'edited', a."isEdited",
  'description', COALESCE(e.description, '') <> '',
  'rating', COALESCE(e.rating, 0) <> 0,
  'locked_metadata', COALESCE(cardinality(e."lockedProperties"), 0) > 0,
  'albums', ARRAY(SELECT "albumId" FROM album_asset WHERE "assetId" = a.id ORDER BY 1),
  'tags', ARRAY(SELECT "tagId" FROM tag_asset WHERE "assetId" = a.id ORDER BY 1),
  'shared', EXISTS(SELECT 1 FROM shared_link_asset WHERE "assetId" = a.id),
  'memory', EXISTS(SELECT 1 FROM memory_asset WHERE "assetId" = a.id),
  'memories', ARRAY(SELECT "memoriesId" FROM memory_asset WHERE "assetId" = a.id ORDER BY 1),
  'custom_metadata', EXISTS(SELECT 1 FROM asset_metadata WHERE "assetId" = a.id),
  'sidecar', EXISTS(SELECT 1 FROM asset_file WHERE "assetId" = a.id AND type = 'sidecar'),
  'sidecar_paths', ARRAY(SELECT path FROM asset_file WHERE "assetId" = a.id AND type = 'sidecar' ORDER BY path),
  'file_reference', COALESCE(cardinality(array_remove(f.ids, a.id)), 0) > 0
)
FROM asset a LEFT JOIN asset_exif e ON e."assetId" = a.id
LEFT JOIN file_paths f ON f.path = a."originalPath"
ORDER BY a.id;
COMMIT;
