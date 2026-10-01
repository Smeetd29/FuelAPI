from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'required': 'Start location is required.',
            'blank': 'Start location cannot be blank.'
        }
    )
    finish = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            'required': 'Finish location is required.',
            'blank': 'Finish location cannot be blank.'
        }
    )

    def validate(self, data):
        start = data.get('start', '').strip()
        finish = data.get('finish', '').strip()

        if not start:
            raise serializers.ValidationError({'start': 'Start location cannot be blank.'})
        if not finish:
            raise serializers.ValidationError({'finish': 'Finish location cannot be blank.'})

        if start.lower() == finish.lower():
            raise serializers.ValidationError(
                {'finish': 'Start and finish locations cannot be the same.'}
            )

        return {'start': start, 'finish': finish}
